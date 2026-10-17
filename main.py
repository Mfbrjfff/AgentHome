"""
Smart Home Autonomous AI Agent Harness — FastAPI server.

- REST API (devices, chat, scheduler, routines, analytics, system)
- WebSocket /ws  → real-time device state + agent thought-log streaming
- Serves the Liquid Glass SPA from /static
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func

from config import settings
from eventbus import bus
from models import (SessionLocal, init_db, ChatMessage, ScheduledJob,
                    HealthMeasurement, RoutineRun)
from tools.registry import load_all, all_tools, invoke
from tools.scheduler import add_job, remove_job, restore_jobs, scheduler
from tools.rpi_monitor import system_snapshot
import routines as rt

BASE = Path(__file__).parent
STATIC = BASE / "static"


# ------------------------------------------------------------------ lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    load_all()                                   # register every tools/*.py module
    import routines  # registers run_routine tool   # noqa: F401
    n = await restore_jobs()
    app.state.ws_clients = set()
    await seed_default_jobs()

    async def broadcast(msg: dict) -> None:
        data = json.dumps(msg, default=str)
        dead = set()
        for ws in list(app.state.ws_clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        app.state.ws_clients -= dead

    bus.subscribe(broadcast)

    # background pollers: keep dashboard tiles alive even without user actions
    tasks = [asyncio.create_task(poll_system()), asyncio.create_task(poll_devices())]
    yield
    for t in tasks:
        t.cancel()
    scheduler.shutdown(wait=False)


async def poll_devices() -> None:
    from tools.hue import hue_status
    from tools.spotify import spotify_status
    from tools.roidmi import roidmi_status
    while True:
        try:
            await hue_status()
            await spotify_status()
            await roidmi_status()
        except Exception:
            pass
        await asyncio.sleep(20)


async def seed_default_jobs() -> None:
    """Register the three flagship smart routines on first boot."""
    async with SessionLocal() as s:
        existing = (await s.execute(select(func.count(ScheduledJob.id)))).scalar() or 0
    if existing:
        return
    await add_job("Morning Routine", "Weather + scale + lights + music briefing",
                  "routine", {"name": "morning"}, "cron",
                  settings.morning_routine_cron, created_by="system")
    await add_job("Evening Wind-Down", "Dim lights, quiet audio, tomorrow's agenda",
                  "routine", {"name": "evening_winddown"}, "cron",
                  settings.evening_winddown_cron, created_by="system")
    if settings.away_clean_enabled:
        await add_job("Away Cleaning Check", "Presence-gated Roidmi dispatch", "routine",
                      {"name": "away_clean"}, "interval",
                      str(settings.away_check_interval_min * 60), created_by="system")


async def poll_system() -> None:
    while True:
        try:
            snap = system_snapshot()
            await bus.emit("system", "vitals", snap, glow=False)
        except Exception:
            pass
        await asyncio.sleep(5)


app = FastAPI(title="Smart Home Autonomous AI Agent Harness", version="1.0.0",
              lifespan=lifespan)


# ------------------------------------------------------------------ websockets
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    app.state.ws_clients.add(ws)
    # initial snapshot so the UI paints instantly
    await ws.send_text(json.dumps({"type": "snapshot", **bus.snapshot(),
                                   "server_time": time.time()}, default=str))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    reply = await _agent_turn(text)
                    await ws.send_text(json.dumps({"type": "chat_reply", "text": reply}))
            elif msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
    except WebSocketDisconnect:
        pass
    finally:
        app.state.ws_clients.discard(ws)


_agent_lock = asyncio.Lock()


async def _agent_turn(text: str) -> str:
    async with _agent_lock:                    # serialise LLM turns on a 4GB Pi
        from agent import agent_handle_message
        return await agent_handle_message(text)


# -------------------------------------------------------------------- routes
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/health")
async def health():
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        pass
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
            "model": settings.ollama_model, "ollama_reachable": ollama_ok,
            "demo_mode": settings.demo_mode,
            "tools": [t.name for t in all_tools()]}


@app.post("/api/chat")
async def chat(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    reply = await _agent_turn(text)
    return {"reply": reply}


@app.get("/api/history")
async def history(limit: int = 100):
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChatMessage).order_by(ChatMessage.id.desc())
                                .limit(limit))).scalars().all()
    return [{"role": r.role, "content": r.content, "tool": r.tool_name, "ts": r.ts}
            for r in reversed(rows)]


@app.post("/api/tool/{name}")
async def call_tool(name: str, args: dict = Body(default={})):
    """Direct quick-action endpoint used by dashboard toggles (bypasses the LLM)."""
    result = await invoke(name, args)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/api/devices")
async def devices():
    return bus.device_state | {"_recent_count": len(bus.history)}


# ---- scheduler CRUD -------------------------------------------------------
@app.get("/api/jobs")
async def jobs():
    async with SessionLocal() as s:
        rows = (await s.execute(select(ScheduledJob).order_by(ScheduledJob.id))).scalars().all()
    return [r.to_dict() for r in rows]


@app.post("/api/jobs")
async def create_job(payload: dict = Body(...)):
    return await add_job(payload.get("name", "Untitled task"),
                         payload.get("description", ""),
                         payload.get("action_type", "agent_prompt"),
                         payload.get("action_payload", {}),
                         payload.get("trigger_kind", "date"),
                         payload.get("trigger_expr", ""),
                         created_by="user")


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    return {"ok": await remove_job(job_id)}


@app.post("/api/jobs/{job_id}/run")
async def run_job_now(job_id: str):
    job = scheduler.get_job(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    asyncio.create_task(job.func(*job.args))
    return {"ok": True}


# ---- routines --------------------------------------------------------------
@app.post("/api/routines/{name}")
async def run_routine(name: str):
    res = await rt.run_routine(name)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/routines")
async def list_routines():
    async with SessionLocal() as s:
        rows = (await s.execute(select(RoutineRun).order_by(RoutineRun.ts.desc())
                                .limit(20))).scalars().all()
    return {"available": list(rt.ROUTINES),
            "runs": [{"ts": r.ts, "name": r.name, "status": r.status,
                      "summary": r.summary} for r in rows]}


# ---- analytics -------------------------------------------------------------
@app.get("/api/analytics/health")
async def analytics_health(days: int = 90):
    since = time.time() - days * 86400
    async with SessionLocal() as s:
        rows = (await s.execute(select(HealthMeasurement)
                                .where(HealthMeasurement.ts >= since)
                                .order_by(HealthMeasurement.ts))).scalars().all()
    series: dict[str, list[list[float]]] = {}
    for r in rows:
        series.setdefault(r.category, []).append([r.ts, r.value])
    return {"days": days, "series": series}


@app.get("/api/analytics/energy")
async def analytics_energy():
    """Estimate home energy from device states (Pi draw + active Hue watts + vacuum)."""
    st = bus.device_state
    hue = st.get("hue", {})
    lights_w = 0.0
    for room, v in (hue.get("lights") or {}).items():
        if isinstance(v, dict) and v.get("on"):
            lights_w += 4 + 7 * (v.get("brightness", 50) / 100)     # LED 4–11 W
    vac = st.get("roidmi", {})
    vac_w = {"cleaning": 55.0, "charging": 30.0, "returning": 45.0}.get(vac.get("state"), 2.0)
    sysd = st.get("system", {})
    total = round(lights_w + vac_w + float(sysd.get("power_est_w", 4)), 1)
    hist = [(time.time() - i * 300, round(total * (0.85 + 0.3 % (i + 1) / 4), 1))
            for i in range(48, 0, -1)]
    return {"now_w": total, "breakdown": {"hue_w": round(lights_w, 1),
                                          "vacuum_w": vac_w,
                                          "pi_w": sysd.get("power_est_w", 4)},
             "history": hist}


# ---- system / network ------------------------------------------------------
@app.get("/api/system")
async def system():
    return system_snapshot()


@app.get("/api/tools")
async def tools():
    return [{"name": t.name, "domain": t.domain, "description": t.description,
             "examples": t.examples} for t in all_tools()]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
