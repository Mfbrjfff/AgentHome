"""
SQLAlchemy (async) models — agent memory, scheduled jobs, health data cache,
routine runs, and settings key/value store.
"""
from __future__ import annotations

import time
import json
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Integer, String, Text, DateTime
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import settings

engine = create_async_engine(settings.db_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class ChatMessage(Base):
    """Persistent conversation memory between the user and the agent."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, default=lambda: time.time())
    role: Mapped[str] = mapped_column(String(16))          # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str] = mapped_column(String(64), default="")


class AgentLogEntry(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, default=lambda: time.time())
    level: Mapped[str] = mapped_column(String(8), default="info")
    kind: Mapped[str] = mapped_column(String(32), default="agent")
    text: Mapped[str] = mapped_column(Text, default="")


class ScheduledJob(Base):
    """Mirror of APScheduler jobs so the UI can list/edit/persist everything,
    including jobs created autonomously by the agent."""
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    action_type: Mapped[str] = mapped_column(String(32))    # tool | routine | agent_prompt
    action_payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    trigger_kind: Mapped[str] = mapped_column(String(16))   # cron | date | interval
    trigger_expr: Mapped[str] = mapped_column(String(128))  # cron str / ISO dt / seconds
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(16), default="user")  # user | agent | system
    next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "name": self.name,
            "description": self.description,
            "action_type": self.action_type,
            "action_payload": json.loads(self.action_payload or "{}"),
            "trigger_kind": self.trigger_kind,
            "trigger_expr": self.trigger_expr,
            "enabled": bool(self.enabled),
            "created_by": self.created_by,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self.last_run.isoformat() if self.last_run else None,
        }


class HealthMeasurement(Base):
    """Cached Withings measurements for trend charts."""
    __tablename__ = "health_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(32))   # weight | fat | bmi | heart_rate
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="withings")


class RoutineRun(Base):
    __tablename__ = "routine_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, default=lambda: time.time())
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    summary: Mapped[str] = mapped_column(Text, default="")


class SettingKV(Base):
    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
