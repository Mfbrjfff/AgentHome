"""
Smart scenario routines — multi-step, context-aware automations that combine
several integrations. Each routine streams its plan into the agent log so the
dashboard terminal shows the whole thought/execution sequence live.

1. morning          — weather + scale sync + Hue wake light + Spotify + summary
2. evening_winddown — dim lights, stop loud playback, preview tomorrow's tasks
3. away_clean       — presence check (+ recent weigh-ins as occupancy proxy) →
                      Roidmi cleaning only if the house is empty
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from eventbus import bus
from models import SessionLocal, RoutineRun, ScheduledJob
from tools.registry import invoke


async def _log(step: str) -> None:
    await bus.emit_log(f"[ROUTINE] {step}", kind="agent")


# ------------------------------------------------------------------ MORNING
async def morning_routine() -> dict:
    t0 = time.time()
    await _log("▶ Morning Routine started — gathering context…")
    parts: list[str] = []

    wx = await invoke("weather_forecast", {})
    if wx.get("ok"):
        parts.append(f"Weather: {wx['condition']}, {wx['temp_c']}°C "
                     f"(feels {wx.get('feels_like')}).")
        await _log(f"✓ Weather checked: {wx['condition']} {wx['temp_c']}°C")

    wt = await invoke("withings_sync", {})
    latest = (wt.get("latest") or {})
    if latest:
        parts.append(f"Latest weigh-in: {latest['weight_kg']} kg, "
                     f"{latest['fat_pct']}% body fat, {latest['heart_rate']} bpm resting HR.")
        await _log("✓ Withings scale synced")

    # Lighting adapts to brightness of the day + time
    hour = datetime.now().hour
    overcast = any(k in str(wx.get("condition", "")) for k in ("rain", "cloud", "overcast"))
    scene = "morning-warm"
    await invoke("hue_scene", {"scene": scene, "room": "all"})
    if overcast or hour < 6:
        await invoke("hue_lights", {"target": "kitchen", "on": True, "brightness": 90,
                                    "color_temp_mireds": 250})
        parts.append("It's grey out there — boosted kitchen light to 90% cool-white.")
    else:
        parts.append("Set 'morning-warm' lighting scene.")
    await _log("✓ Hue lights adjusted for the morning")

    sp = await invoke("spotify_playlist", {"operation": "play_named", "name": "morning-vibes"})
    if sp.get("ok"):
        parts.append("Started your 'Morning Vibes' playlist on the Kitchen Google Home Hub.")
    await _log("✓ Spotify morning playlist queued")

    news = await invoke("rss_digest", {"limit_per_feed": 3})
    if news.get("ok"):
        heads = [i["title"] for items in news["feeds"].values() for i in items][:3]
        if heads:
            parts.append("Top headlines: " + " | ".join(heads))
    await _log("✓ News digest prepared")

    summary = "Good morning! ☀️ " + " ".join(parts)
    # Announce on Google Home if HA is configured
    await invoke("ha_service_call", {"domain": "tts", "service": "google_translate_say",
                                     "entity_id": "media_player.kitchen_hub",
                                     "data": {"message": summary[:240]}})
    return await _finish("morning", summary, t0)


# ------------------------------------------------------------ EVENING WIND-DOWN
async def evening_winddown() -> dict:
    t0 = time.time()
    await _log("▶ Evening Wind-Down started…")
    parts: list[str] = []

    await invoke("hue_scene", {"scene": "relax-evening", "room": "all"})
    await asyncio_sleep(0.2)
    await invoke("hue_lights", {"target": "office", "on": False})
    parts.append("Dimmed all lights to the 'relax-evening' candle-warm scene and switched off the office.")
    await _log("✓ Hue dimmed for wind-down")

    st = await invoke("spotify_status", {})
    if st.get("is_playing"):
        vol = st.get("volume") or 0
        if vol > 30:
            await invoke("spotify_playback", {"action": "set_volume", "volume": 25})
            parts.append(f"Lowered music volume from {vol}% to a calm 25%.")
        else:
            parts.append("Music was already at a relaxed level — left it playing.")
    else:
        parts.append("No active playback to quieten.")
    await _log("✓ Audio environment settled")

    async with SessionLocal() as s:
        rows = (await s.execute(select(ScheduledJob).where(ScheduledJob.enabled == 1)))\
            .scalars().all()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    upcoming = [r.name for r in rows if r.next_run and
                r.next_run.replace(tzinfo=timezone.utc).date() == tomorrow]
    if upcoming:
        parts.append("Tomorrow you have: " + ", ".join(upcoming[:6]) + ".")
        await _log(f"✓ Tomorrow's agenda: {', '.join(upcoming[:6])}")
    else:
        parts.append("Nothing scheduled for tomorrow — a free day.")

    summary = "🌙 Evening wind-down complete. " + " ".join(parts)
    return await _finish("evening_winddown", summary, t0)


# ------------------------------------------------------------- AWAY CLEANING
_last_away_clean = {"ts": 0.0}
AWAY_COOLDOWN_S = 4 * 3600      # don't nag-clean more than every 4h


async def away_clean_routine(force: bool = False) -> dict:
    t0 = time.time()
    await _log("▶ Away-Cleaning check started…")

    pres = await invoke("presence_check", {})
    home = pres.get("home") or []
    await _log(f"Presence source: {pres.get('source')} → {len(home)} device(s) home")

    # Secondary signal: a weigh-in within the last 2h strongly implies someone is home
    recent_weigh_in = False
    cutoff = time.time() - 2 * 3600
    try:
        from models import HealthMeasurement
        async with SessionLocal() as s:
            latest_meas = (await s.execute(
                select(HealthMeasurement).where(HealthMeasurement.category == "weight")
                .order_by(HealthMeasurement.ts.desc()).limit(1))).scalars().first()
        recent_weigh_in = bool(latest_meas and latest_meas.ts > cutoff)
    except Exception:
        pass

    if home or recent_weigh_in:
        why = "people detected home" if home else "recent scale activity"
        await _log(f"✗ Skipping vacuum run — {why}.")
        return {"ok": True, "ran": False,
                "summary": f"Cleaning skipped ({why}). House is occupied."}

    if not force and time.time() - _last_away_clean["ts"] < AWAY_COOLDOWN_S:
        await _log("✗ Within cooldown window of previous away-clean; skipping.")
        return {"ok": True, "ran": False, "summary": "Skipped: recently cleaned already."}

    vac = await invoke("roidmi_control", {"action": "start"})
    if not vac.get("ok"):
        await _log(f"✗ Vacuum refused to start: {vac.get('error')}")
        return {"ok": False, "ran": False, "summary": f"Vacuum could not start: {vac.get('error')}"}

    _last_away_clean["ts"] = time.time()
    summary = ("🏠 The house is empty — Roidmi Eve dispatched for a full cleaning round. "
               "It will auto-return to dock when finished.")
    await _log("✓ Robot vacuum dispatched autonomously")
    return await _finish("away_clean", summary, t0, ran=True)


async def _finish(name: str, summary: str, t0: float, ran: bool = True) -> dict:
    dur = round(time.time() - t0, 2)
    async with SessionLocal() as s:
        s.add(RoutineRun(name=name, status="ok", summary=summary))
        await s.commit()
    await bus.emit("agent", "routine_done", {"name": name, "summary": summary, "duration_s": dur},
                   log_line=f"[ROUTINE] ■ '{name}' completed in {dur}s")
    return {"ok": True, "ran": ran, "name": name, "summary": summary, "duration_s": dur}


async def asyncio_sleep(x: float) -> None:
    import asyncio
    await asyncio.sleep(x)


ROUTINES = {
    "morning": morning_routine,
    "evening_winddown": evening_winddown,
    "away_clean": away_clean_routine,
}


async def run_routine(name: str) -> dict:
    fn = ROUTINES.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown routine '{name}'. Known: {list(ROUTINES)}"}
    return await fn()


# ------------------------------------------------------- agent-facing tool
from tools.registry import register  # noqa: E402


@register(
    name="run_routine",
    domain="agent",
    description="Execute a multi-step smart scenario. Routines: "
                "'morning' (weather+scale+lights+music+summary), "
                "'evening_winddown' (dim lights, quiet audio, tomorrow's agenda), "
                "'away_clean' (presence-gated robot vacuum).",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": ["morning", "evening_winddown", "away_clean"]},
        },
        "required": ["name"],
    },
    examples=["run the morning routine", "start evening wind down"],
)
async def run_routine_tool(name: str) -> dict:
    return await run_routine(name)
