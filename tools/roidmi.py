"""
Roidmi Eve Plus robot vacuum — Mi Home / python-miio protocol integration.
Live mode uses `micloud`+`miiot` style commands over the local miIO UDP
protocol; demo mode simulates cleaning sessions with battery drain, room
coverage progress and dock returns so the dashboard blue ripple works.
"""
from __future__ import annotations

import asyncio
import random
import time

from config import settings
from eventbus import bus
from .registry import register

ROOMS = ["living-room", "kitchen", "bedroom", "hallway", "office"]

_SIM = {
    "state": "idle",              # idle | cleaning | returning | charging | error
    "battery": 87,
    "dustbin_level": 0.22,        # 0..1 fill ratio
    "mode": "quiet",              # quiet | standard | turbo
    "current_room": None,
    "cleaned_area_m2": 0.0,
    "session_started": None,
    "total_runtime_min": 1240,
}
_clean_task: asyncio.Task | None = None


def _live() -> bool:
    return bool(settings.roidmi_device_ip and settings.roidmi_device_token) and not settings.demo_mode


async def _miio_command(cmd: str, params=None):
    """Best-effort local miIO command via python-miio's DeviceFactory (sync lib
    wrapped in a thread so the event loop never blocks)."""
    try:
        from miio import Device  # type: ignore
        import anyio

        def _run():
            dev = Device(settings.roidmi_device_ip, settings.roidmi_device_token)
            if cmd == "start":
                return dev.send("set_mode", [{"mode": "auto", "fan_power": 90}])
            if cmd == "stop":
                return dev.send("set_mode", [{"mode": "stand"}])
            if cmd == "home":
                return dev.send("set_charge", [{"value": True}])
            if cmd == "status":
                return dev.status().data
            return dev.send(cmd, params or [])
        return await anyio.to_thread.run_sync(_run)
    except Exception:
        return None


async def _emit_state(event: str, log: str | None = None) -> None:
    await bus.emit("roidmi", event, dict(_SIM), log_line=log)


async def _simulate_clean(rooms: list[str] | None):
    global _clean_task
    targets = rooms or ROOMS
    _SIM["state"] = "cleaning"
    _SIM["session_started"] = time.time()
    _SIM["cleaned_area_m2"] = 0.0
    await _emit_state("cleaning_started",
                      f"[ROIDMI] Cleaning session started → {', '.join(targets)}")
    for room in targets:
        if _SIM["state"] != "cleaning":
            return
        _SIM["current_room"] = room
        for _ in range(4):
            if _SIM["state"] != "cleaning":
                return
            await asyncio.sleep(1.2)
            _SIM["cleaned_area_m2"] += round(random.uniform(2.5, 6.0), 1)
            _SIM["battery"] = max(5, _SIM["battery"] - random.randint(1, 2))
            _SIM["dustbin_level"] = min(1.0, _SIM["dustbin_level"] + random.uniform(0.01, 0.04))
        await _emit_state("room_done", f"[ROIDMI] Finished '{room}' "
                        f"({_SIM['cleaned_area_m2']} m² total, batt {_SIM['battery']}%)")
    await roidmi_control("return")


@register(
    name="roidmi_control",
    domain="roidmi",
    description="Control the Roidmi Eve robot vacuum: start cleaning (optionally "
                "specific rooms), stop, return to dock/charge, set suction mode.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["start", "stop", "return", "set_mode", "spot"]},
            "rooms": {"type": "array", "items": {"type": "string"},
                      "description": "Rooms to clean: " + ", ".join(ROOMS)},
            "mode": {"type": "string", "enum": ["quiet", "standard", "turbo"]},
        },
        "required": ["action"],
    },
    examples=["start the vacuum", "clean only the kitchen", "send the robot back to dock"],
)
async def roidmi_control(action: str, rooms: list[str] | None = None,
                         mode: str | None = None) -> dict:
    global _clean_task
    if _live():
        res = await _miio_command(action, {"mode": mode} if mode else None)
        await _emit_state(action, f"[ROIDMI] {action} sent to device: {res}")
        return {"ok": res is not None, "response": res}

    if action == "start":
        if _SIM["state"] == "cleaning":
            return {"ok": False, "error": "Vacuum is already cleaning"}
        if _SIM["battery"] < 20:
            await _emit_state("blocked_low_battery",
                              f"[ROIDMI] Refusing to clean: battery {_SIM['battery']}% < 20%. "
                              "Returning to dock.")
            await roidmi_control("return")
            return {"ok": False, "error": "Battery too low; docking instead"}
        bad = [r for r in (rooms or []) if r not in ROOMS]
        if bad:
            return {"ok": False, "error": f"Unknown rooms {bad}. Known: {ROOMS}"}
        _clean_task = asyncio.create_task(_simulate_clean(rooms))
        return {"ok": True, "state": dict(_SIM), "simulated": True}
    if action == "stop":
        _SIM["state"] = "idle"
        await _emit_state("stopped", "[ROIDMI] Cleaning stopped by user/agent")
    elif action == "return":
        _SIM["state"] = "returning"
        await _emit_state("returning", "[ROIDMI] Returning to dock")
        await asyncio.sleep(2.0)
        _SIM["state"] = "charging"
        await _emit_state("docking", "[ROIDMI] Docked — charging")
    elif action == "spot":
        _SIM["state"] = "cleaning"
        _SIM["current_room"] = "spot-zone"
        await _emit_state("spot_clean", "[ROIDMI] Spot cleaning started")
    elif action == "set_mode":
        _SIM["mode"] = mode or "standard"
        await _emit_state("mode_changed", f"[ROIDMI] Suction mode → {_SIM['mode']}")
    return {"ok": True, "state": dict(_SIM), "simulated": True}


@register(
    name="roidmi_status",
    domain="roidmi",
    description="Read Roidmi vacuum status: state, battery %, dustbin fill level, mode, runtime.",
    parameters={"type": "object", "properties": {}},
)
async def roidmi_status() -> dict:
    if _live():
        st = await _miio_command("status")
        if st:
            _SIM.update({"battery": st.get("battery", _SIM["battery"]),
                         "state": st.get("stage", _SIM["state"])})
        await _emit_state("status", None)
        return {"ok": True, "state": st or dict(_SIM)}
    # trickle passive updates so the tile feels alive even when idle
    if _SIM["state"] == "charging":
        _SIM["battery"] = min(100, _SIM["battery"] + 1)
    await _emit_state("status", None)
    return {"ok": True, "state": dict(_SIM), "simulated": True}
