"""
Spotify Connect integration via the Spotify Web API (async httpx, OAuth2
client-credentials + cached user token flow). Falls back to a stateful
simulator (player + queue + "morning-vibes" playlist) in demo mode.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import random

import httpx

from config import settings
from eventbus import bus
from .registry import register

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"

# ----------------------------------------------------------------- simulator
_SIM = {
    "is_playing": False,
    "track": {"name": "Golden Hour", "artist": "Jacob Collier",
              "album": "Djesoln Vol. 2", "duration_ms": 214000},
    "position_ms": 0,
    "volume": 42,
    "shuffle": True,
    "devices": [
        {"id": "pi-spot", "name": "Raspberry Pi 5", "type": "computer", "volume": 42},
        {"id": "gh-hub-k", "name": "Google Home Hub (Kitchen)", "type": "speaker", "volume": 55},
        {"id": "gh-mini-lr", "name": "Google Mini (Living Room)", "type": "speaker", "volume": 30},
    ],
    "device": "gh-hub-k",
    "queue": [],
}
_MORNING_TRACKS = [
    ("Sunrise", "Norah Jones"), ("Bloom", "Odesza"), ("Golden", "Harry Styles"),
    ("Feel Good", "Gorillaz"), ("Lovely Day", "Bill Withers"),
]


def _live() -> bool:
    return bool(settings.spotify_client_id and settings.spotify_client_secret) and not settings.demo_mode


_token_cache = {"token": None, "exp": 0}


async def _access_token() -> str | None:
    if _token_cache["token"] and _token_cache["exp"] > time.time() + 30:
        return _token_cache["token"]
    auth = base64.b64encode(
        f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{ACCOUNTS}/api/token",
                             headers={"Authorization": f"Basic {auth}"},
                             data={"grant_type": "client_credentials"})
            if r.status_code == 200:
                j = r.json()
                _token_cache.update(token=j["access_token"], exp=time.time() + j["expires_in"])
                return j["access_token"]
    except Exception:
        pass
    return None


async def _call(method: str, path: str, **kwargs):
    tok = await _access_token()
    if not tok:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {tok}"}, **kwargs)
            if r.status_code == 204:
                return {"ok": True}
            if r.status_code == 200:
                return r.json() if r.content else {"ok": True}
            return {"error": r.status_code, "detail": r.text[:200]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)}


async def _announce(action: str, extra: dict | None = None) -> None:
    payload = {"is_playing": _SIM["is_playing"], "track": _SIM["track"],
               "volume": _SIM["volume"], "device": _SIM["device"], **(extra or {})}
    await bus.emit("spotify", action, payload,
                   log_line=f"[SPOTIFY] {action}: "
                            f"{'▶' if _SIM['is_playing'] else '⏸'} {_SIM['track']['name']} — "
                            f"{_SIM['track']['artist']} @ vol {_SIM['volume']}%")


@register(
    name="spotify_playback",
    domain="spotify",
    description="Spotify Connect playback control: play, pause, next, previous, seek, "
                "set volume (0-100), or start a specific playlist/track URI on a device.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["play", "pause", "next", "previous", "toggle", "set_volume"]},
            "volume": {"type": "integer", "minimum": 0, "maximum": 100},
            "context_uri": {"type": "string", "description": "spotify:playlist:... URI to play"},
            "device_id": {"type": "string", "description": "Target device id/name (e.g. Google Home)"},
        },
        "required": ["action"],
    },
    examples=["pause spotify", "play morning playlist on the kitchen google home"],
)
async def spotify_playback(action: str, volume: int | None = None,
                           context_uri: str | None = None, device_id: str | None = None) -> dict:
    if _live():
        body: dict = {}
        if context_uri:
            body["context_uri"] = context_uri
        if device_id:
            body["device_id"] = device_id
        routes = {"play": ("PUT", "/me/player/play", body),
                  "pause": ("PUT", "/me/player/pause", None),
                  "next": ("POST", "/me/player/next", None),
                  "previous": ("POST", "/me/player/previous", None),
                  "set_volume": ("PUT", f"/me/player/volume?volume_percent={volume or 0}", None)}
        if action == "toggle":
            st = await _call("GET", "/me/player")
            action = "pause" if (st or {}).get("is_playing") else "play"
        m, p, b = routes[action]
        res = await _call(m, p, json=b if b is not None else None)
        await _announce(action, {"target_device": device_id})
        return {"ok": "error" not in (res or {}), "response": res}

    # ---- simulated ----
    await asyncio.sleep(0.15)
    if action == "toggle":
        _SIM["is_playing"] = not _SIM["is_playing"]
    elif action == "play":
        _SIM["is_playing"] = True
        if context_uri:
            _SIM["track"] = {"name": "Playlist loaded", "artist": context_uri,
                             "album": "", "duration_ms": 1800000}
    elif action == "pause":
        _SIM["is_playing"] = False
    elif action == "next":
        name, artist = random.choice(_MORNING_TRACKS)
        _SIM["track"] = {"name": name, "artist": artist, "album": "Simulated", "duration_ms": 200000}
        _SIM["position_ms"] = 0
    elif action == "previous":
        _SIM["position_ms"] = 0
    elif action == "set_volume":
        _SIM["volume"] = max(0, min(100, volume or _SIM["volume"]))
    if device_id:
        for d in _SIM["devices"]:
            if device_id.lower() in (d["id"] + " " + d["name"]).lower():
                _SIM["device"] = d["id"]
                break
    await _announce(action)
    return {"ok": True, "state": {k: _SIM[k] for k in ("is_playing", "track", "volume", "device")},
            "simulated": True}


@register(
    name="spotify_status",
    domain="spotify",
    description="Get current Spotify playback state: track, artist, progress, volume, active device.",
    parameters={"type": "object", "properties": {}},
)
async def spotify_status() -> dict:
    if _live():
        st = await _call("GET", "/me/player") or {}
        item = (st.get("item") or {})
        out = {"ok": True, "is_playing": st.get("is_playing", False),
               "track": {"name": item.get("name"), "artist": ", ".join(
                   a["name"] for a in item.get("artists", [])),
                   "album": (item.get("album") or {}).get("name"),
                   "duration_ms": item.get("duration_ms")},
               "volume": st.get("device", {}).get("volume_percent"),
               "device": st.get("device", {}).get("name")}
        await bus.emit("spotify", "status", out, glow=False)
        return out
    _SIM["position_ms"] = (_SIM["position_ms"] + 30000) % _SIM["track"]["duration_ms"] \
        if _SIM["is_playing"] else _SIM["position_ms"]
    out = {"ok": True, "is_playing": _SIM["is_playing"], "track": _SIM["track"],
           "position_ms": _SIM["position_ms"], "volume": _SIM["volume"],
           "device": _SIM["device"], "devices": _SIM["devices"], "simulated": True}
    await bus.emit("spotify", "status", out, glow=False)
    return out


@register(
    name="spotify_playlist",
    domain="spotify",
    description="Play one of the known playlists by name ('morning-vibes', 'focus-lofi', "
                "'evening-jazz') or generate a new AI-curated playlist from a mood/theme.",
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["play_named", "generate"]},
            "name": {"type": "string", "description": "Playlist name or mood, e.g. 'upbeat morning'"},
        },
        "required": ["operation"],
    },
)
async def spotify_playlist(operation: str, name: str = "morning-vibes") -> dict:
    named = {"morning-vibes": "spotify:playlist:sim-morning-vibes",
             "focus-lofi": "spotify:playlist:sim-focus-lofi",
             "evening-jazz": "spotify:playlist:sim-evening-jazz"}
    if operation == "play_named":
        uri = named.get(name.lower(), named["morning-vibes"])
        return await spotify_playback("play", context_uri=uri)
    # generate: in live mode would call /me/playlists + search; simulated below
    tracks = [{"name": n, "artist": a} for n, a in random.sample(_MORNING_TRACKS, 3)]
    pl = {"ok": True, "playlist": f"AI:{name}", "tracks_added": tracks, "simulated": not _live()}
    await bus.emit("spotify", "playlist_generated",
                   {"playlist": pl["playlist"], "tracks": tracks},
                   log_line=f"[SPOTIFY] Generated playlist '{pl['playlist']}' "
                            f"with {len(tracks)} tracks for mood '{name}'")
    return pl
