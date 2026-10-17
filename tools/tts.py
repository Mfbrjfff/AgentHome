"""
Text-to-Speech engine — the agent's voice.

There is NO speech-to-text anywhere in this harness (the deployment has no
microphone); input is always typed/triggered, output may be spoken.

Three parallel output channels, each optional & auto-detected:
  1. Dashboard speakers  → every agent reply is pushed over WebSocket as a
     `speak` message and rendered with the browser Web Speech API (app.js).
  2. Pi's own audio jack / BT speaker → espeak-ng / Piper subprocess fallback
     so Atlas can talk even when nobody has the dashboard open.
  3. Google Home hubs    → via Home Assistant TTS service (ha_home tool).
"""
from __future__ import annotations

import asyncio
import re
import shutil

from config import settings
from eventbus import bus
from tools.registry import tool

_TTS_SHELL_DENY = re.compile(r"rm\s+-rf|\bmkfs\b|dd\s+if=", re.I)


def _clean(text: str) -> str:
    """Strip markdown / emoji so the synthesiser doesn't read 'hash star rocket'."""
    text = re.sub(r"[*_#`>~]", " ", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: settings.tts_max_chars]


def _local_synth() -> list[str] | None:
    """Best available on-device CLI synthesiser, or None."""
    for cmd in (["espeak-ng", "-v", "en-gb", "-s", "165"],
                ["espeak", "-v", "en-gb", "-s", "165"],
                ["piper", "--model", settings.tts_voice]):
        if shutil.which(cmd[0]):
            if cmd[0] == "piper":
                return ["piper", "-m", settings.tts_voice or "en_GB-alba-medium.onnx"]
            return cmd
    return None


async def speak_local(text: str) -> dict:
    synth = _local_synth()
    if not synth:
        return {"ok": False, "error": "no local synthesiser (apt install espeak-ng)"}
    proc = await asyncio.create_subprocess_exec(
        *synth, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(_clean(text).encode()), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "synthesis timed out"}
    return {"ok": proc.returncode == 0, "engine": synth[0]}


async def broadcast_speak(text: str, voice_hint: str = "") -> None:
    """Push to every connected dashboard client (browser TTS)."""
    await bus.emit("tts", "speak",
                   {"text": _clean(text), "voice": voice_hint or settings.tts_voice,
                    "rate": settings.tts_rate, "pitch": settings.tts_pitch,
                    "lang": settings.tts_lang}, glow=False)


@tool("speak",
      "Say something out loud right now through the home's voice channel: the "
      "dashboard speakers (browser TTS), the Raspberry Pi's own audio output "
      "(espeak-ng/Piper) and optionally announced on Google Home via Home "
      "Assistant. Use it for greetings, alerts and routine summaries.",
      {"type": "object", "properties": {
          "text": {"type": "string", "description": "sentence(s) to speak"},
          "google_home": {"type": "boolean",
                          "description": "also broadcast on Google Home hub",
                          "default": False},
          "entity_id": {"type": "string",
                        "description": "HA media_player/TTS entity for broadcast",
                        "default": "media_player.kitchen_hub"}},
       "required": ["text"]},
      domain="tts",
      examples=["say dinner is ready on the speakers"])
async def speak(text: str, google_home: bool = False,
                entity_id: str = "media_player.kitchen_hub") -> dict:
    clean = _clean(text)
    if not clean:
        return {"ok": False, "error": "nothing to say"}
    out: dict = {"ok": True, "text": clean}
    await broadcast_speak(clean)                       # 1) dashboard browsers
    out["dashboard"] = True

    res = await speak_local(clean)                     # 2) Pi onboard audio
    out["local"] = res
    if not res.get("ok"):
        out["note"] = res.get("error")

    if google_home:                                    # 3) Google Home via HA
        from tools.ha_home import ha_service_call
        r = await ha_service_call(domain="tts", service="google_translate_say",
                                  entity_id=entity_id, data={"message": clean})
        out["google_home"] = r
    await bus.emit_log(f"🔊 TTS: “{clean[:80]}”", kind="tts")
    return out


@tool("tts_config",
      "View or change voice settings (language, rate 0.5-2, pitch 0.5-2, max chars, "
      "enabled flag, whether replies are auto-spoken). Applies live to dashboard speech.",
      {"type": "object", "properties": {
          "enabled": {"type": "boolean"},
          "auto_reply": {"type": "boolean",
                         "description": "automatically speak every agent chat reply"},
          "lang": {"type": "string", "description": "BCP-47 e.g. en-US, lv-LV, de-DE"},
          "rate": {"type": "number", "minimum": 0.5, "maximum": 2},
          "pitch": {"type": "number", "minimum": 0.5, "maximum": 2},
          "voice": {"type": "string", "description": "preferred voice name hint"},
          "max_chars": {"type": "integer", "minimum": 100, "maximum": 3000}},
       "required": []},
      domain="tts")
async def tts_config(enabled: bool | None = None, auto_reply: bool | None = None,
                     lang: str | None = None, rate: float | None = None,
                     pitch: float | None = None, voice: str | None = None,
                     max_chars: int | None = None) -> dict:
    if enabled is not None:
        settings.tts_enabled = bool(enabled)
    if auto_reply is not None:
        settings.tts_auto_reply = bool(auto_reply)
    if lang:
        settings.tts_lang = lang
    if rate is not None:
        settings.tts_rate = max(0.5, min(2.0, float(rate)))
    if pitch is not None:
        settings.tts_pitch = max(0.5, min(2.0, float(pitch)))
    if voice is not None:
        settings.tts_voice = voice
    if max_chars is not None:
        settings.tts_max_chars = int(max_chars)
    await bus.emit("tts", "config", tts_state(), glow=False)
    return {"ok": True, **tts_state()}


def tts_state() -> dict:
    return {"enabled": settings.tts_enabled,
            "auto_reply": getattr(settings, "tts_auto_reply", True),
            "lang": settings.tts_lang, "rate": settings.tts_rate,
            "pitch": settings.tts_pitch, "voice": settings.tts_voice,
            "max_chars": settings.tts_max_chars,
            "local_engine": (_local_synth() or [None])[0]}
