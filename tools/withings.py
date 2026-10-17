"""
Withings smart scale integration (OAuth2 / long-lived token Data API).
Syncs weight, fat %, BMI, heart rate into the local DB for trend charts.
Demo mode generates a realistic 90-day downward-trending body-composition
dataset plus fresh "measurements" on each sync so green pulses appear.
"""
from __future__ import annotations

import asyncio
import math
import random
import time

import httpx
from sqlalchemy import select

from config import settings
from eventbus import bus
from models import SessionLocal, HealthMeasurement
from .registry import register

LIVE = bool(settings.withings_access_token and settings.withings_user_id) and not settings.demo_mode
_NOTIFY = "https://wbsapi.withings.net/v2/measure"

_seed_weight = 78.4 + random.uniform(-0.3, 0.3)
_SYNC_CACHE: dict = {"latest": None}


def _generate_history(days: int = 90) -> list[dict]:
    """Realistic simulated history: slow loss + weekly water-weight oscillation."""
    out = []
    now = time.time()
    height_m = 1.78
    for d in range(days, 0, -1):
        if random.random() < 0.25:      # not everyone weighs daily
            continue
        t = now - d * 86400
        base = _seed_weight - (days - d) * 0.018          # ~-1.6 kg over 90d
        wave = 0.35 * math.sin(d / 7 * 2 * math.pi)       # weekly fluctuation
        noise = random.gauss(0, 0.12)
        weight = round(base + wave + noise, 2)
        fat = round(19.5 - (days - d) * 0.008 + random.gauss(0, 0.2), 1)
        out.append({"ts": int(t), "weight_kg": weight, "fat_pct": fat,
                    "bmi": round(weight / (height_m ** 2), 1),
                    "heart_rate": random.randint(52, 68)})
    return out


async def _store(measurements: list[dict]) -> None:
    async with SessionLocal() as s:
        for m in measurements:
            for cat, key in (("weight", "weight_kg"), ("fat", "fat_pct"),
                             ("bmi", "bmi"), ("heart_rate", "heart_rate")):
                if m.get(key) is not None:
                    s.add(HealthMeasurement(ts=m["ts"], category=cat, value=float(m[key])))
        await s.commit()


async def _fetch_live() -> list[dict]:
    params = {"action": "getintraday", "userid": settings.withings_user_id,
              "category": 1, "startdate": int(time.time()) - 90 * 86400}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_NOTIFY, data=params,
                             headers={"Authorization": f"Bearer {settings.withings_access_token}"})
            j = r.json()
            if j.get("status") != 0:
                return []
            out = []
            for item in j["body"].get("series", {}).get("tracks", []):
                fields: dict = {}
                for a in item.get("attributes", []):
                    if a.get("type") == 6:                       # WeightSum (grams)
                        fields["weight_kg"] = round(a["value"] / 1000, 2)
                    elif a.get("type") == 7:                     # FatMassRatio
                        fields["fat_pct"] = round(a["value"] * 100, 1)
                    elif a.get("type") == 5:                     # HeartRate
                        fields["heart_rate"] = a["value"]
                if fields.get("weight_kg"):
                    fields.setdefault("bmi", round(fields["weight_kg"] / 1.78 ** 2, 1))
                    fields["ts"] = item.get("date", int(time.time()))
                    out.append(fields)
            return out
    except Exception:
        return []


@register(
    name="withings_sync",
    domain="withings",
    description="Synchronise the latest Withings scale measurements (weight, body fat %, "
                "BMI, resting heart rate) from the cloud into local memory.",
    parameters={"type": "object", "properties": {}},
    examples=["sync my scale data", "check my latest weight"],
)
async def withings_sync() -> dict:
    await bus.emit_log("[WITHINGS] Starting measurement sync…", kind="withings")
    if LIVE:
        meas = await _fetch_live()
    else:
        await asyncio.sleep(0.4)
        hist = _generate_history(90)
        meas = hist[-14:]                       # recent window synced
        # brand-new weigh-in appears on every sync in demo mode
        fresh = {"ts": int(time.time()),
                 "weight_kg": round(_seed_weight - 1.5 + random.gauss(0, .15), 2),
                 "fat_pct": round(18.8 + random.gauss(0, .2), 1),
                 "heart_rate": random.randint(52, 64)}
        fresh["bmi"] = round(fresh["weight_kg"] / 1.78 ** 2, 1)
        meas.append(fresh)
    await _store(meas)
    latest = max(meas, key=lambda m: m["ts"]) if meas else None
    _SYNC_CACHE["latest"] = latest
    payload = {"count": len(meas), "latest": latest}
    log = (f"[WITHINGS] Sync OK — {len(meas)} readings. Latest: {latest['weight_kg']} kg, "
           f"{latest['fat_pct']}% fat, {latest['heart_rate']} bpm"
           if latest else "[WITHINGS] No new data")
    await bus.emit("withings", "synced", payload, log_line=log)
    return {"ok": True, **payload, "simulated": not LIVE}


@register(
    name="withings_trends",
    domain="withings",
    description="Analyse Withings health trends over the last N days: averages, deltas and "
                "direction of weight/fat/heart-rate. Use before giving health insights.",
    parameters={
        "type": "object",
        "properties": {"days": {"type": "integer", "default": 30}},
    },
)
async def withings_trends(days: int = 30) -> dict:
    since = time.time() - days * 86400
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(HealthMeasurement).where(HealthMeasurement.ts >= since)
            .order_by(HealthMeasurement.ts))).scalars().all()
    series: dict[str, list[float]] = {}
    stamps: dict[str, list[float]] = {}
    for r in rows:
        series.setdefault(r.category, []).append(r.value)
        stamps.setdefault(r.category, []).append(r.ts)
    if not series:                      # first call before any sync — seed db
        await withings_sync()
        return await withings_trends(days)
    stats = {}
    for cat, vals in series.items():
        half = max(1, len(vals) // 2)
        delta = round(sum(vals[half:]) / len(vals[half:]) - sum(vals[:half]) / half, 2) \
            if len(vals) > 1 else 0.0
        stats[cat] = {"latest": vals[-1], "avg": round(sum(vals) / len(vals), 2),
                      "min": min(vals), "max": max(vals), "delta_period": delta,
                      "points": len(vals)}
    await bus.emit("withings", "trends", stats, glow=False)
    return {"ok": True, "days": days, "stats": stats, "series": series,
            "timestamps": stamps, "simulated": not LIVE}
