"""
Proactive Scheduler & Task Executor (APScheduler + SQLAlchemy persistence).

Both the user (via REST/UI) and the AI agent (via the `schedule_task` tool)
can create reminders, one-time jobs, cron jobs and recurring routines.
Job actions are: call a registered tool, run a named routine, or feed an
instruction back to the agent ("agent_prompt").
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from eventbus import bus
from models import SessionLocal, ScheduledJob
from .registry import register, invoke

scheduler = AsyncIOScheduler(timezone="UTC")
scheduler.start()


# ---------------------------------------------------------------- executors
async def _execute_job(job_id: str) -> None:
    """Resolve a persisted job and run its action."""
    async with SessionLocal() as s:
        row = (await s.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id)))\
            .scalar_one_or_none()
        if row is None:
            return
        payload = json.loads(row.action_payload or "{}")
        row.last_run = datetime.utcnow()
        await s.commit()

    await bus.emit_log(f"[SCHEDULER] Firing job '{row.name}' ({job_id})", kind="system")
    result: dict
    if row.action_type == "tool":
        result = await invoke(payload.get("tool", ""), payload.get("args", {}))
    elif row.action_type == "routine":
        from routines import run_routine           # late import avoids cycle
        result = await run_routine(payload.get("name", ""))
    elif row.action_type == "agent_prompt":
        from agent import agent_handle_message     # late import avoids cycle
        reply = await agent_handle_message(payload.get("prompt", ""), persist=False)
        result = {"ok": True, "reply": reply}
    else:
        result = {"ok": False, "error": f"unknown action_type {row.action_type}"}

    await bus.emit("system", "job_executed",
                   {"job_id": job_id, "name": row.name, "result": result},
                   log_line=f"[SCHEDULER] '{row.name}' finished: "
                            f"{'OK' if result.get('ok') else result.get('error')}")


def _build_trigger(kind: str, expr: str):
    if kind == "cron":
        return CronTrigger.from_crontab(expr)
    if kind == "date":
        return DateTrigger(run_date=datetime.fromisoformat(expr))
    if kind == "interval":
        return IntervalTrigger(seconds=int(float(expr)))
    raise ValueError(f"Unknown trigger kind '{kind}'")


async def add_job(name: str, description: str, action_type: str, action_payload: dict,
                  trigger_kind: str, trigger_expr: str, created_by: str = "user") -> dict:
    job_id = uuid.uuid4().hex[:12]
    try:
        trigger = _build_trigger(trigger_kind, trigger_expr)
    except Exception as e:
        return {"ok": False, "error": f"Invalid trigger: {e}"}
    ap = scheduler.add_job(_execute_job, trigger, args=[job_id], id=job_id,
                          name=name, misfire_grace_time=300, coalesce=True)
    async with SessionLocal() as s:
        s.add(ScheduledJob(job_id=job_id, name=name, description=description,
                           action_type=action_type,
                           action_payload=json.dumps(action_payload),
                           trigger_kind=trigger_kind, trigger_expr=trigger_expr,
                           created_by=created_by,
                           next_run=ap.next_run_time.replace(tzinfo=None) if ap.next_run_time else None))
        await s.commit()
    await bus.emit("system", "job_added", {"job_id": job_id, "name": name},
                   log_line=f"[SCHEDULER] Job '{name}' created by {created_by} "
                            f"({trigger_kind}={trigger_expr})")
    return {"ok": True, "job_id": job_id, "next_run": str(ap.next_run_time)}


async def remove_job(job_id: str) -> bool:
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    async with SessionLocal() as s:
        row = (await s.execute(select(ScheduledJob).where(ScheduledJob.job_id == job_id)))\
            .scalar_one_or_none()
        if row:
            await s.delete(row)
            await s.commit()
            await bus.emit("system", "job_removed", {"job_id": job_id},
                           log_line=f"[SCHEDULER] Removed job {job_id}")
            return True
    return False


async def restore_jobs() -> int:
    """Re-register persisted jobs into APScheduler on boot."""
    n = 0
    async with SessionLocal() as s:
        rows = (await s.execute(select(ScheduledJob))).scalars().all()
    for row in rows:
        if not row.enabled:
            continue
        try:
            trigger = _build_trigger(row.trigger_kind, row.trigger_expr)
        except Exception:
            continue
        if row.trigger_kind == "date":
            # one-shot jobs whose time already passed are skipped
            try:
                if datetime.fromisoformat(row.trigger_expr) < datetime.utcnow():
                    continue
            except ValueError:
                continue
        scheduler.add_job(_execute_job, trigger, args=[row.job_id], id=row.job_id,
                          name=row.name, misfire_grace_time=300, coalesce=True)
        n += 1
    return n


# ---------------------------------------------------------------- tools
@register(
    name="schedule_task",
    domain="scheduler",
    description=(
        "Create a reminder or automated task. Choose trigger_kind: "
        "'date' (ISO datetime for one-time, e.g. 'in 30 minutes' -> now+30m ISO), "
        "'cron' (5-field crontab, e.g. '0 8 * * 1-5' weekdays 08:00), "
        "or 'interval' (seconds between runs). Choose action_type: "
        "'tool' (payload {'tool': <registered tool name>, 'args': {...}}), "
        "'routine' (payload {'name': 'morning'|'evening_winddown'|'away_clean'}), "
        "or 'agent_prompt' (payload {'prompt': 'instruction you want yourself to execute later'}). "
        "Registered tools: hue_lights, hue_scene, spotify_playback, spotify_playlist, "
        "roidmi_control, withings_sync, weather_forecast, rss_digest, web_search."),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short human title"},
            "description": {"type": "string", "default": ""},
            "trigger_kind": {"type": "string", "enum": ["date", "cron", "interval"]},
            "trigger_expr": {"type": "string",
                             "description": "ISO datetime | crontab string | seconds"},
            "action_type": {"type": "string", "enum": ["tool", "routine", "agent_prompt"]},
            "action_payload": {"type": "object",
                               "description": "e.g. {'tool':'hue_scene','args':{'scene':'morning-warm'}}"},
        },
        "required": ["name", "trigger_kind", "trigger_expr", "action_type", "action_payload"],
    },
    examples=["remind me to take out trash in 30 minutes",
              "every weekday 8am turn on lights and play morning playlist"],
)
async def schedule_task(name: str, trigger_kind: str, trigger_expr: str,
                        action_type: str, action_payload: dict,
                        description: str = "") -> dict:
    return await add_job(name, description, action_type, action_payload,
                         trigger_kind, trigger_expr, created_by="agent")


@register(
    name="list_tasks",
    domain="scheduler",
    description="List all scheduled tasks/reminders with their next run times.",
    parameters={"type": "object", "properties": {}},
)
async def list_tasks() -> dict:
    async with SessionLocal() as s:
        rows = (await s.execute(select(ScheduledJob).order_by(ScheduledJob.id))).scalars().all()
    return {"ok": True, "tasks": [r.to_dict() for r in rows]}


@register(
    name="cancel_task",
    domain="scheduler",
    description="Cancel/delete a scheduled task by its job_id (from list_tasks).",
    parameters={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
)
async def cancel_task(job_id: str) -> dict:
    ok = await remove_job(job_id)
    return {"ok": ok, "error": None if ok else f"job {job_id} not found"}
