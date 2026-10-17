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


def tool(name: str, description: str, parameters: dict, domain: str = "general",
         examples: list[str] | None = None):
    """Positional-args sugar for register() — used by dynamic skill modules."""
    return register(name=name, description=description, parameters=parameters,
                    domain=domain, examples=examples)


def unregister_prefix(prefix: str) -> list[str]:
    """Remove every tool whose name starts with `prefix` (skill cleanup)."""
    removed = [n for n in _REGISTRY if n.startswith(prefix)]
    for n in removed:
        del _REGISTRY[n]
    return removed


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
    from . import (hue, spotify, roidmi, withings, websearch, scheduler,  # noqa: F401
                   ha_home, rpi_monitor, skills, selfmod, tts)            # noqa: F401
    load_installed_skills()


def load_installed_skills() -> int:
    """Import every ./skills/*.py file so previously installed skills are live
    right after boot (hot-reloadable later via the skill_reload tool)."""
    import importlib.util
    import sys
    from pathlib import Path
    from config import settings
    sdir = Path(settings.skills_dir)
    if not sdir.exists():
        return 0
    n = 0
    for p in sorted(sdir.glob("*.py")):
        mod_name = f"skills.{p.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, p)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
                n += 1
        except Exception as e:  # a broken skill must never block boot
            print(f"[registry] skill {p.name} failed to load: {e}")
    return n
