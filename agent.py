"""
Autonomous AI Agent harness around the local Ollama `bonsai-2:4b` model.

Implements a full function-calling / tool-selection loop against Ollama's
native /api/chat tool protocol, with a robust JSON-mode fallback for builds
where native tool calling is unavailable. Every reasoning step, tool call and
result is streamed onto the EventBus so the dashboard terminal shows the
agent "thinking" in real time.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from config import settings
from eventbus import bus
from models import SessionLocal, ChatMessage
from tools.registry import invoke, ollama_tool_specs, all_tools

SYSTEM_PROMPT = f"""You are Atlas, an autonomous AI agent that runs a smart home on a Raspberry Pi 5.
Current local time: {{now}}.
You control: Philips Hue lights, Spotify Connect (incl. Google Home speakers),
a Roidmi Eve robot vacuum, Withings smart scales, Home Assistant entities,
web search / weather / RSS news, a persistent task scheduler, system vitals,
Bluetooth devices (scan/connect/disconnect), and your own voice (speak tool).

YOU HAVE MAXIMUM AUTONOMY — this is deliberate:
- You may MODIFY YOURSELF: read_file/edit_file rewrite your own source code
  (tools/, skills/, static/ dashboard, routines.py, agent.py); use list_project
  to explore, restore_backup if something breaks, self_restart to activate changes.
- You may EXTEND yourself: write_skill authors brand-new tools in Python;
  skill_install pulls ready-made skills from GitHub or ClawHub URLs; skill_list/skill_reload manage them.
- You may run shell diagnostics with run_shell, control Bluetooth hardware, and freely
  create/update/delete scheduled jobs (list_jobs, schedule_task, cancel_job) without asking permission.
- Your replies are spoken aloud via text-to-speech automatically — keep them speakable:
  short sentences, no markdown symbols, no emoji, plain natural language.

Guidelines:
- ALWAYS use tools to act or fetch facts instead of guessing; you cannot control devices by chat alone.
- For multi-step goals, chain tool calls (e.g. check presence BEFORE dispatching the vacuum).
- When asked to remember/remind/automate something, use schedule_task with correct trigger math
  ('in 30 minutes' -> trigger_kind=date with ISO timestamp now+30m; 'every weekday 8am' -> cron '0 8 * * 1-5').
- If the user asks for a capability you lack, CREATE it: prefer skill_install from GitHub/ClawHub,
  otherwise write_skill, then verify by calling the new tool.
- Prefer acting over explaining. Keep final answers concise, warm and human. Mention which devices you changed.
- If a tool errors, explain briefly and suggest a fix rather than retrying endlessly.
"""


def _system_prompt() -> str:
    return SYSTEM_PROMPT.format(now=datetime.now().strftime("%A %Y-%m-%d %H:%M"))


async def _ollama_chat(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5)) as c:
        r = await c.post(f"{settings.ollama_base_url}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()


async def _remember(role: str, content: str, tool_name: str = "") -> None:
    async with SessionLocal() as s:
        s.add(ChatMessage(role=role, content=content, tool_name=tool_name))
        await s.commit()


async def load_memory(max_messages: int = 24) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id.desc())
                                .limit(max_messages))).scalars().all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)
            if r.role in ("user", "assistant")]


# ---------------------------------------------------------- fallback parsing
_JSON_TOOL_RE = re.compile(r"\{[^{}]*\"tool\"[^{}]*\}", re.S)


async def _fallback_think(messages: list[dict]) -> dict:
    """For Ollama builds without native tools: ask the model to answer with
    either plain text or strict JSON {"tool": name, "args": {...}}."""
    tool_names = ", ".join(t.name for t in all_tools())
    req = dict(messages=messages + [{
        "role": "system",
        "content": f"To call a tool reply ONLY with JSON: {{\"tool\": \"<name>\", \"args\": {{...}}}}. "
                   f"Available tools: {tool_names}. Otherwise reply normally in text."}],
        model=settings.ollama_model, stream=False, keep_alive="10m",
        options={"temperature": settings.llm_temperature, "num_ctx": 4096})
    resp = await _ollama_chat(req)
    content = resp.get("message", {}).get("content", "").strip()
    m = _JSON_TOOL_RE.search(content)
    if m:
        try:
            j = json.loads(m.group(0))
            if j.get("tool") in {t.name for t in all_tools()}:
                return {"tool_calls": [{"function": {"name": j["tool"],
                                                     "arguments": j.get("args", {})}}]}
        except json.JSONDecodeError:
            pass
    return {"content": content or "…"}


# ------------------------------------------------------------ main entry
async def agent_handle_message(user_text: str, persist: bool = True) -> str:
    """Run one full agentic turn: LLM → tool calls → observations → final answer."""
    if persist:
        await _remember("user", user_text)
    await bus.emit_log(f"🧠 Agent received: “{user_text[:120]}”", kind="agent")

    messages = ([{"role": "system", "content": _system_prompt()}]
                + await load_memory())
    tools = ollama_tool_specs()
    final_text = ""

    for iteration in range(settings.max_tool_iterations):
        try:
            resp = await _ollama_chat({
                "model": settings.ollama_model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "keep_alive": "30m",
                "options": {"temperature": settings.llm_temperature,
                            "num_ctx": 4096, "num_predict": settings.llm_max_tokens},
            })
            msg = resp.get("message", {})
        except Exception as e:
            # Ollama down or no tool support → JSON-mode fallback once, then give up politely
            await bus.emit_log(f"⚠ Ollama native call failed ({e}); trying JSON tool fallback",
                               level="warn", kind="agent")
            try:
                msg = await _fallback_think(messages)
                msg = msg.get("message", msg) if "message" in msg else msg
            except Exception as e2:
                return (f"⚠ I can't reach the local model at {settings.ollama_base_url} "
                        f"({type(e2).__name__}). Start it with `ollama serve` and pull "
                        f"`{settings.ollama_model}`.")

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if not tool_calls:
            final_text = content or "(no response)"
            break

        # The model decided to think out loud before acting — surface it
        if content:
            await bus.emit_log(f"💭 {content[:200]}", kind="agent")
        messages.append(msg)

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            await bus.emit_log(f"🔧 CALL {name}({json.dumps(args, default=str)[:180]})",
                               kind="agent")
            t0 = time.time()
            result = await invoke(name, args)
            dur = round((time.time() - t0) * 1000)
            ok = result.get("ok", False)
            await bus.emit_log(
                f"↩︎ {name} → {'OK' if ok else 'ERROR'} ({dur} ms): "
                f"{json.dumps(result, default=str)[:220]}",
                level="info" if ok else "error", kind="agent")
            await _remember("tool", json.dumps(result, default=str)[:2000], tool_name=name)
            messages.append({"role": "tool", "name": name,
                             "content": json.dumps(result, default=str)[:3000]})
    else:
        final_text = final_text or "I hit my tool-call budget for this request — here's what I managed so far."

    if not final_text:
        final_text = "Done."
    await bus.emit_log(f"✅ Agent replied to user ({len(final_text)} chars)", kind="agent")
    if persist:
        await _remember("assistant", final_text)
    # ── Text-to-Speech: speak the reply (no speech-to-text — no microphone) ──
    if getattr(settings, "tts_enabled", True) and getattr(settings, "tts_auto_reply", True):
        try:
            from tools.tts import broadcast_speak
            await broadcast_speak(final_text)
        except Exception as e:  # TTS must never break the chat loop
            await bus.emit_log(f"⚠ TTS broadcast failed: {e}", level="warn", kind="tts")
    return final_text
