"""
Philips Hue Bridge integration (CLIP API v2, with stateful simulator fallback).
When demo_mode is on or the bridge is unreachable, a simulated light group
keeps the agent loop, dashboard tiles and liquid-glow effects fully functional.
"""
from __future__ import annotations

import asyncio
import random

import httpx

from config import settings
from eventbus import bus
from .registry import register

_HEADERS_V2 = {"X-Api-Key": settings.hue_app_username}

# ----------------------------------------------------------------- simulator
_SIM_LIGHTS = {
    "Living Room": {"on": False, "brightness": 80, "color_temp_mireds": 330},
    "Kitchen":     {"on": False, "brightness": 90, "color_temp_mireds": 300},
    "Bedroom":     {"on": False, "brightness": 40, "color_temp_mireds": 450},
    "Hallway":     {"on": False, "brightness": 60, "color_temp_mireds": 370},
    "Office":      {"on": False, "brightness": 95, "color_temp_mireds": 180},
}
SCENE_PRESETS = {
    "morning-warm":  {"on": True, "brightness": 75, "color_temp_mireds": 300},
    "relax-evening": {"on": True, "brightness": 25, "color_temp_mireds": 450},
    "focus-daylight":{"on": True, "brightness": 95, "color_temp_mireds": 180},
    "candlelight":   {"on": True, "brightness": 15, "color_temp_mireds": 500},
    "goodnight":     {"on": False},
}


def _live() -> bool:
    return bool(settings.hue_bridge_ip and settings.hue_app_username) and not settings.demo_mode


async def _v2_request(method: str, path: str, body=None):
    url = f"http://{settings.hue_bridge_ip}/clip/v2/resource/{path}"
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.request(method, url, headers=_HEADERS_V2,
                                json=[body] if (method == "PUT" and body) else None)
            if r.status_code in (200, 207):
                return r.json()
    except Exception:
        return None
    return None


def _match(name: str, target: str) -> bool:
    return target == "all" or target.lower() in name.lower() or name.lower() in target.lower()


@register(
    name="hue_lights",
    domain="hue",
    description="Control Philips Hue lights: power on/off, brightness percent (0-100), "
                "color temperature in mireds (153=cool daylight, 300=warm, 500=candle). "
                "Target can be a room/light name or 'all'.",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Room/light name or 'all'"},
            "on": {"type": "boolean", "description": "Power state"},
            "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
            "color_temp_mireds": {"type": "integer", "minimum": 153, "maximum": 500},
            "transition_ms": {"type": "integer", "default": 400},
        },
        "required": ["target"],
    },
    examples=["turn on the living room lights", "dim bedroom to 20% warm"],
)
async def hue_lights(target: str, on: bool | None = None, brightness: int | None = None,
                     color_temp_mireds: int | None = None, transition_ms: int = 400) -> dict:
    changed: dict[str, dict] = {}

    if _live():
        data = await _v2_request("GET", "light")
        if data is None:
            return {"ok": False, "error": "Hue bridge unreachable"}
        for res in data.get("data", []):
            name = res.get("metadata", {}).get("name", res["id"])
            if not _match(name, target):
                continue
            d: dict = {"transitionduration": max(1, transition_ms // 100)}
            if on is not None:
                d["on"] = {"on": on}
            if brightness is not None:
                d["dimming"] = {"brightness": brightness}
            if color_temp_mireds is not None:
                d["color_temp"] = {"mireds": color_temp_mireds}
            out = await _v2_request("PUT", f"light/{res['id']}", {"id": res["id"], **d})
            if out:
                changed[name] = d
    else:
        for name, st in _SIM_LIGHTS.items():
            if not _match(name, target):
                continue
            if on is not None:
                st["on"] = on
            if brightness is not None:
                st["brightness"] = max(0, min(100, brightness))
            if color_temp_mireds is not None:
                st["color_temp_mireds"] = color_temp_mireds
            changed[name] = dict(st)
            await asyncio.sleep(0.04)  # simulate staggered fade transitions

    if not changed:
        return {"ok": False, "error": f"No lights matched target '{target}'"}

    any_on = any(s["on"] for s in _SIM_LIGHTS.values())
    avg_b = round(sum(v.get("brightness", 50) for v in changed.values()) / len(changed))
    await bus.emit("hue", "lights_changed",
                   {"target": target, "changed": changed, "any_on": any_on,
                    "avg_brightness": avg_b, "lights": _SIM_LIGHTS if not _live() else None},
                   log_line=f"[HUE] {target}: " + ", ".join(
                       f"{k} -> {'on' if v.get('on', _SIM_LIGHTS.get(k, {}).get('on')) else 'off'}"
                       f" @ {v.get('brightness', _SIM_LIGHTS.get(k, {}).get('brightness'))}%"
                       for k, v in changed.items()))
    return {"ok": True, "affected": changed, "simulated": not _live()}


@register(
    name="hue_scene",
    domain="hue",
    description="Activate a named Hue scene across a room. Known scenes: "
                + ", ".join(SCENE_PRESETS),
    parameters={
        "type": "object",
        "properties": {
            "scene": {"type": "string", "enum": list(SCENE_PRESETS)},
            "room": {"type": "string", "default": "all"},
        },
        "required": ["scene"],
    },
)
async def hue_scene(scene: str, room: str = "all") -> dict:
    preset = SCENE_PRESETS.get(scene)
    if preset is None:
        return {"ok": False, "error": f"Unknown scene '{scene}'. Known: {list(SCENE_PRESETS)}"}
    res = await hue_lights(target=room, transition_ms=1500, **preset)
    if res.get("ok"):
        await bus.emit("hue", "scene_activated", {"scene": scene, "room": room},
                       log_line=f"[HUE] Scene '{scene}' activated for '{room}'")
    return res


@register(
    name="hue_status",
    domain="hue",
    description="Read current Philips Hue light states (power, brightness, color temp).",
    parameters={"type": "object", "properties": {}},
)
async def hue_status() -> dict:
    if _live():
        data = await _v2_request("GET", "light")
        if data is None:
            return {"ok": False, "error": "Hue bridge unreachable"}
        lights = {r["metadata"]["name"]: r for r in data.get("data", [])}
        await bus.emit("hue", "status", {"lights": lights}, glow=False)
        return {"ok": True, "lights": lights}
    await bus.emit("hue", "status", {"lights": _SIM_LIGHTS}, glow=False)
    return {"ok": True, "lights": _SIM_LIGHTS, "simulated": True}
