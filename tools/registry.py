"""
Tool registry — every integration module registers async callables here with
an OpenAI/Ollama-compatible JSON schema. The agent loop in agent.py builds its
tool manifest from this registry, and main.py exposes it to the dashboard.
Adding a new integration = drop a file into tools/ and @register it.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON-schema (type: object)
    func: Callable[..., Awaitable[Any]]
    domain: str = "general"               # hue | spotify | roidmi | ...
    examples: list[str] = field(default_factory=list)


_REGISTRY: dict[str, Tool] = {}


def register(name: str, description: str, parameters: dict, domain: str = "general",
             examples: list[str] | None = None):
    """Decorator that registers an async tool function."""
    def deco(fn: Callable[..., Awaitable[Any]]):
        assert inspect.iscoroutinefunction(fn), f"tool {name} must be async"
        _REGISTRY[name] = Tool(name=name, description=description, parameters=parameters,
                               func=fn, domain=domain, examples=examples or [])
        return fn
    return deco


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def ollama_tool_specs() -> list[dict]:
    """Tools in Ollama /api/chat native tool-calling format."""
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description,
                      "parameters": t.parameters}}
        for t in _REGISTRY.values()
    ]


async def invoke(name: str, arguments: dict | str | None) -> dict:
    """Execute a registered tool; normalises results & traps errors."""
    tool = _REGISTRY.get(name)
    if tool is None:
        return {"ok": False, "error": f"Unknown tool '{name}'"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
    try:
        result = await tool.func(**(arguments or {}))
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return result
    except TypeError as e:
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001 - the agent loop must survive tool crashes
        return {"ok": False, "error": f"{type(e).__name__} in {name}: {e}"}


def load_all() -> None:
    """Import AFTER register() exists so modules can decorate at import time."""
    from . import hue, spotify, roidmi, withings, websearch, scheduler, ha_home, rpi_monitor  # noqa: F401
