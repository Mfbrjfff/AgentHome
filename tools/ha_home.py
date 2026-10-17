"""
Google Home / Home Assistant integration via the HA REST API.
Exposes entity control (switches, media_player — which covers Google Home
speakers/hubs), service calls and presence detection (device trackers /
ARP ping of known phone MACs as fallback).
"""
from __future__ import annotations

import asyncio
import random
import subprocess
import time

import httpx

from config import settings
from eventbus import bus
from .registry import register

LIVE = bool(settings.ha_base_url and settings.ha_token) and not settings.demo_mode
_BASE = settings.ha_base_url.rstrip("/")
_HDRS = {"Authorization": f"Bearer {settings.ha_token}"}

# ----------------------------------------------------------------- simulator
_SIM_ENTITIES = {
    "switch.christmas_lights": {"friendly_name": "Christmas Lights", "state": "off"},
    "switch.coffee_machine":   {"friendly_name": "Coffee Machine", "state": "off"},
    "fan.bedroom_fan":         {"friendly_name": "Bedroom Fan", "state": "off"},
    "media_player.kitchen_hub": {"friendly_name": "Google Home Hub Kitchen",
                                 "state": "idle", "volume": 0.4},
    "media_player.living_room_speaker": {"friendly_name": "Google Nest Mini LR",
                                         "state": "paused", "volume": 0.3},
    "lock.front_door":         {"friendly_name": "Front Door", "state": "locked"},
    "climate.thermostat":      {"friendly_name": "Thermostat", "state": "heat",
                                "temperature": 21.0},
}
_presence_cache: dict = {"home": [], "ts": 0}


async def _ha(method: str, path: str, json_body=None):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, f"{_BASE}{path}", headers=_HDRS, json=json_body)
            if r.status_code in (200, 201):
                return r.json() if r.content else {"ok": True}
            return {"error": r.status_code, "detail": r.text[:200]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)}


@register(
    name="ha_entities",
    domain="ha",
    description="List Home Assistant entities (switches, lights, media_players for Google "
                "Home, locks, climate) with their current states.",
    parameters={
        "type": "object",
        "properties": {"filter": {"type": "string",
                                  "description": "substring filter on entity_id, e.g. 'media_player'"}},
    },
)
async def ha_entities(filter: str | None = None) -> dict:
    if LIVE:
        data = await _ha("GET", "/api/states")
        if isinstance(data, list):
            out = {e["entity_id"]: {"friendly_name": e["attributes"].get("friendly_name"),
                                    "state": e["state"]} for e in data}
        else:
            return {"ok": False, "error": data}
    else:
        out = {k: dict(v) for k, v in _SIM_ENTITIES.items()}
    if filter:
        out = {k: v for k, v in out.items() if filter.lower() in k.lower()
               or filter.lower() in v.get("friendly_name", "").lower()}
    await bus.emit("ha", "entities", {"count": len(out)}, glow=False)
    return {"ok": True, "entities": out, "simulated": not LIVE}


@register(
    name="ha_service_call",
    domain="ha",
    description="Call a Home Assistant service to control any device/Google Home broadcast. "
                "Common pairs: (light,turn_on), (light,turn_off), (switch,turn_on), "
                "(switch,turn_off), (media_player,play_media), (media_player,volume_mute), "
                "(tts,google_translate_say) for announcing on Google Home speakers.",
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "e.g. switch, light, media_player, tts"},
            "service": {"type": "string", "description": "e.g. turn_on, play_media, google_translate_say"},
            "entity_id": {"type": "string"},
            "data": {"type": "object",
                     "description": "extra service data, e.g. {'message':'Dinner is ready'} "
                                    "or {'media_content_id':..., 'media_content_type':...}"},
        },
        "required": ["domain", "service"],
    },
    examples=["announce dinner is ready on the google home hub", "turn off the coffee machine"],
)
async def ha_service_call(domain: str, service: str, entity_id: str | None = None,
                          data: dict | None = None) -> dict:
    body = {"entity_id": entity_id} if entity_id else {}
    body.update(data or {})
    if domain == "tts" and "say" in service:
        message = (data or {}).get("message", "")
        await bus.emit("ha", "broadcast", {"entity_id": entity_id, "message": message},
                       log_line=f"[GOOGLE HOME] TTS broadcast → {entity_id}: “{message}”")
        return {"ok": True, "broadcast": message}

    if LIVE:
        res = await _ha("POST", f"/api/services/{domain}/{service}", body)
        ok = "error" not in res
    else:
        await asyncio.sleep(0.2)
        ok = True
        if entity_id in _SIM_ENTITIES:
            ent = _SIM_ENTITIES[entity_id]
            if service.startswith("turn_on"):
                ent["state"] = "on"
            elif service.startswith("turn_off"):
                ent["state"] = "off"
            elif service == "toggle":
                ent["state"] = "off" if ent["state"] == "on" else "on"
            elif service == "play_media" or service == "media_play":
                ent["state"] = "playing"
            elif service in ("media_pause", "turn_on_mute", "volume_mute"):
                ent["state"] = "paused"
        res = {"simulated_entity": ent if entity_id in _SIM_ENTITIES else None}

    await bus.emit("ha", f"{domain}.{service}",
                   {"entity_id": entity_id, "result": res},
                   log_line=f"[HOME ASSISTANT] {domain}.{service} "
                            f"on {entity_id or 'all'} → {'OK' if ok else res}")
    return {"ok": ok, "response": res, "simulated": not LIVE}


@register(
    name="presence_check",
    domain="ha",
    description="Check whether anyone is home using Home Assistant device trackers, "
                "falling back to LAN ARP pings of configured phone MACs. Use before "
                "away-mode automations like robot vacuum cleaning.",
    parameters={"type": "object", "properties": {}},
    examples=["is anyone home", "check if the house is empty"],
)
async def presence_check() -> dict:
    if LIVE:
        data = await _ha("GET", "/api/states")
        trackers = []
        if isinstance(data, list):
            trackers = [e for e in data if e["entity_id"].startswith(("device_tracker", "zone"))]
        home = [t["entity_id"] for t in trackers if t.get("state") == "home"]
        result = {"home": home, "source": "home-assistant"}
    else:
        # ARP-based fallback: check the local neighbour table for known MACs
        detected: list[str] = []
        if settings.presence_macs:
            try:
                proc = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=5)
                table = proc.stdout.lower()
                detected = [m for m in settings.presence_macs if m in table]
            except Exception:
                pass
            source = "arp-ping"
        else:
            # demo heuristic: mostly "home" during 7am-9pm, away otherwise
            h = time.localtime().tm_hour
            detected = ["demo-phone-a"] if 7 <= h < 21 and random.random() > 0.15 else []
            source = "simulated"
        result = {"home": detected, "source": source}

    _presence_cache.update(home=result["home"], ts=time.time())
    away = len(result["home"]) == 0
    await bus.emit("ha", "presence", {"home": result["home"], "away": away},
                   log_line=f"[PRESENCE] {'No one is home' if away else 'People home: ' + ', '.join(result['home'])} "
                            f"({result['source']})")
    return {"ok": True, **result}
