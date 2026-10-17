"""
Self-modification tools — maximum agent autonomy.

Atlas can read and rewrite its OWN source code (tools/, routines.py, agent.py,
static/ dashboard, config defaults), restart itself after code changes, run
shell diagnostics, and even register brand-new behaviour via ./skills/.

Guard rails kept deliberately minimal (the operator opted into full autonomy):
  * writes restricted to the project tree + skills dir;
  * every write is backed up first under .selfmod_backups/ (timestamped);
  * Python files are syntax-checked before being committed;
  * a static deny-list blocks obviously catastrophic shell/source patterns;
  * everything is announced on the event bus so the human can watch & roll back.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import settings
from eventbus import bus
from tools.registry import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJECT_ROOT / ".selfmod_backups"
BACKUP_DIR.mkdir(exist_ok=True)

# directories/files the agent may touch with edit_file / read_file
ALLOWED_WRITE_ROOTS = [
    PROJECT_ROOT / "tools",
    PROJECT_ROOT / "static",
    Path(settings.skills_dir).resolve(),
    PROJECT_ROOT / "routines.py",
    PROJECT_ROOT / "agent.py",
    PROJECT_ROOT / "main.py",
    PROJECT_ROOT / "eventbus.py",
    PROJECT_ROOT / "models.py",
]
DENY_SOURCE = [r"rm\s+(-[rf]{1,2}\s+)*/(?!home)", r"\bmkfs\b", r"dd\s+if=",
               r">\s*/dev/sd", r":\(\)\{.*\};:", r"\bfork\s*\(\)"]


def _within_project(p: Path) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    return any(rp == root or str(rp).startswith(str(root) + os.sep)
               for root in ALLOWED_WRITE_ROOTS)


def _resolve(rel: str) -> Path:
    p = Path(rel)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _backup(path: Path) -> str | None:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"{stamp}_{path.name}"
    shutil.copy2(path, dest)
    return dest.name


@tool("read_file",
      "Read a text file from the harness project (tools/*.py, static/*, routines.py, "
      "agent.py, main.py, skills/*). Use before editing.",
      {"type": "object", "properties": {
          "path": {"type": "string", "description": "project-relative path"},
          "max_bytes": {"type": "integer", "default": 20000}},
       "required": ["path"]},
      domain="selfmod")
async def read_file(path: str, max_bytes: int = 20000) -> dict:
    p = _resolve(path)
    if not _within_project(p):
        return {"ok": False, "error": "path outside editable project areas"}
    if not p.exists() or p.is_dir():
        listing = ([c.name for c in sorted(p.iterdir())][:80]
                   if p.is_dir() else None)
        return {"ok": False, "error": "not found", "dir_listing": listing}
    data = p.read_bytes()[:max_bytes]
    return {"ok": True, "path": str(p.relative_to(PROJECT_ROOT)),
            "size": p.stat().st_size, "content": data.decode(errors="replace")}


@tool("edit_file",
      "Create or overwrite one of your own source files (Python modules under tools/, "
      "your skills, dashboard files under static/, routines.py, agent.py, main.py). "
      "Python files are syntax-checked first; the previous version is backed up "
      "automatically. After editing tools/, call self_restart to load the new code.",
      {"type": "object", "properties": {
          "path": {"type": "string", "description": "project-relative path, e.g. tools/my_ext.py"},
          "content": {"type": "string", "description": "full new file content"}},
       "required": ["path", "content"]},
      domain="selfmod",
      examples=["add a new tool that reads the air quality sensor"])
async def edit_file(path: str, content: str) -> dict:
    p = _resolve(path)
    if not _within_project(p):
        return {"ok": False, "error": "refused: path outside editable project areas"}
    hits = [pat for pat in DENY_SOURCE if re.search(pat, content)]
    if hits:
        return {"ok": False, "error": f"blocked by safety scan: {hits}"}
    if p.suffix == ".py":
        try:
            compile(content, str(p), "exec")
        except SyntaxError as e:
            return {"ok": False, "error": f"syntax error, file NOT written: {e}"}
    backup = _backup(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    await bus.emit_log(f"🛠 Atlas edited {p.relative_to(PROJECT_ROOT)} "
                       f"(backup: {backup or 'new file'})", kind="selfmod")
    await bus.emit("selfmod", "file_edited",
                   {"path": str(p.relative_to(PROJECT_ROOT)), "backup": backup}, glow=False)
    return {"ok": True, "written": str(p.relative_to(PROJECT_ROOT)),
            "bytes": len(content.encode()), "backup": backup,
            "note": "Call self_restart to activate changed Python modules."}


@tool("run_shell",
      "Run a short shell command on the Raspberry Pi host and return stdout/stderr. "
      "For diagnostics and small maintenance tasks (systemctl status, journalctl, "
      "lsusb, ffmpeg, apt list…). Destructive patterns are blocked.",
      {"type": "object", "properties": {
          "command": {"type": "string"},
          "timeout_s": {"type": "integer", "default": 25, "maximum": 120}},
       "required": ["command"]},
      domain="selfmod")
async def run_shell(command: str, timeout_s: int = 25) -> dict:
    hits = [pat for pat in DENY_SOURCE if re.search(pat, command)]
    if hits:
        return {"ok": False, "error": f"blocked by safety scan: {hits}"}
    timeout_s = max(1, min(120, int(timeout_s)))
    proc = await asyncio.create_subprocess_shell(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": f"timed out after {timeout_s}s"}
    text = (out or b"").decode(errors="replace")[-6000:]
    await bus.emit_log(f"🖥 shell $ {command[:90]} → rc={proc.returncode}", kind="selfmod")
    return {"ok": proc.returncode == 0, "rc": proc.returncode, "output": text}


@tool("self_restart",
      "Restart the whole harness process to activate code you changed with edit_file "
      "(or skill installs that touched core modules). Scheduled jobs & chat memory "
      "persist in SQLite and survive the restart.",
      {"type": "object", "properties": {
          "reason": {"type": "string", "description": "why you are restarting"}},
       "required": []},
      domain="selfmod")
async def self_restart(reason: str = "code update") -> dict:
    await bus.emit_log(f"♻️ Atlas is restarting ({reason}) — be back in a few seconds.",
                       level="warn", kind="selfmod")
    await bus.emit("selfmod", "restart", {"reason": reason}, glow=False)
    await asyncio.sleep(1.2)                     # let WS broadcast flush

    def _spawn():
        try:                                   # systemd-managed → clean exit = respawn
            os._exit(0)
        except Exception:                      # fallback: detached exec of main.py
            log = open(PROJECT_ROOT / "server.log", "ab")
            subprocess.Popen([sys.executable, str(PROJECT_ROOT / "main.py")],
                             stdout=log, stderr=log, cwd=PROJECT_ROOT,
                             start_new_session=True)
            os._exit(0)
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, _spawn)
    return {"ok": True, "restarting": True, "reason": reason}


@tool("list_project",
      "List the harness's own file tree (top level + tools/ + skills/) so you know "
      "what you can read/edit.",
      {"type": "object", "properties": {}}, domain="selfmod")
async def list_project() -> dict:
    def ls(d: Path):
        return sorted(f.name + ("/" if f.is_dir() else "") for f in d.iterdir()
                      if not f.name.startswith((".", "__")))
    return {"ok": True, "root": ls(PROJECT_ROOT), "tools": ls(PROJECT_ROOT / "tools"),
            "skills": ls(Path(settings.skills_dir)) if Path(settings.skills_dir).exists() else [],
            "backups": sorted(os.listdir(BACKUP_DIR))[-15:]}


@tool("restore_backup",
      "Roll one of your self-edits back using the automatic timestamped backup "
      "(name shown by edit_file, e.g. 20261017-153000_hue.py).",
      {"type": "object", "properties": {
          "backup_name": {"type": "string"},
          "target_path": {"type": "string",
                          "description": "project-relative file to restore into"}},
       "required": ["backup_name", "target_path"]},
      domain="selfmod")
async def restore_backup(backup_name: str, target_path: str) -> dict:
    src = BACKUP_DIR / re.sub(r"[^A-Za-z0-9._-]", "_", backup_name)
    dst = _resolve(target_path)
    if not src.exists():
        return {"ok": False, "error": f"backup {src.name} not found"}
    if not _within_project(dst):
        return {"ok": False, "error": "target outside editable areas"}
    shutil.copy2(src, dst)
    await bus.emit_log(f"⏪ Restored {dst.name} from backup {src.name}", kind="selfmod")
    return {"ok": True, "restored": str(dst.relative_to(PROJECT_ROOT)), "from": src.name}
