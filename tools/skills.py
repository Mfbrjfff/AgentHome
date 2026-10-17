"""
Skills — the agent's self-extension system.

The agent (and the dashboard) can install Python "skill" files from GitHub or
ClawHub into ./skills/. Each skill is a plain async-Python module that calls
`@tool(...)` from tools.registry to register one or more new capabilities —
they become instantly available in the agent's tool manifest, no restart needed.

Safety model (pragmatic for a trusted LAN device):
  * only .py files are accepted;
  * static deny-list scan for obviously destructive shell patterns;
  * every install/uninstall/reload is announced on the event bus + log;
  * skills run with the same privileges as the harness process itself
    (the user opted into maximum agent autonomy via SH_AGENT_MAX_AUTONOMY).
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

from config import settings
from eventbus import bus
from tools.registry import tool

SKILLS_DIR = Path(settings.skills_dir).resolve()
SKILLS_DIR.mkdir(parents=True, exist_ok=True)
if str(SKILLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SKILLS_DIR.parent))       # allow `import skills.foo`
(SKILLS_DIR / "__init__.py").touch(exist_ok=True)

# source → (host regex, URL template)
_HOSTS = {
    "github": (re.compile(r"(?:https?://)?(?:www\.)?github\.com/|^github[:/]", re.I),
               "https://raw.githubusercontent.com/{repo}/HEAD/{path}"),
    "clawhub": (re.compile(r"(?:https?://)?(?:www\.)?clawhub\.(?:ai|com)/|^clawhub[:/]", re.I),
                "https://clawhub.ai/api/v1/skills/{slug}/download"),
}
_SLUG_RE = re.compile(r"[A-Za-z0-9._-]+")
_DENY_PATTERNS = [
    r"rm\s+(-[rf]{1,2}\s+)*/",       # rm -rf /
    r"\bmkfs\b", r"dd\s+if=", r">\s*/dev/sd",
    r"\bshutdown\b", r"\breboot\b", r"fork\s*\(\)",
]


def _detect_source(ref: str) -> tuple[str, str]:
    """Return (source_key, normalised_ref) for github/clawhub refs."""
    ref = ref.strip().strip("'\"")
    for key, (rx, _) in _HOSTS.items():
        if rx.search(ref):
            return key, ref
    # shorthand: owner/repo[/path] → github
    if re.fullmatch(r"[\w.-]+/[\w.-]+(/.*)?", ref):
        return "github", ref
    return "", ref


def _github_url(ref: str) -> str | None:
    """owner/repo/path/file.py | full github URL → raw URL (branch optional)."""
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+)(?:/(.*))?", f" {ref}")
    if not m:
        m = re.fullmatch(r"([\w.-]+/[\w.-]+)/(.+)", ref.strip())
        if not m:
            return None
        repo, path = m.group(1), m.group(2)
    else:
        repo, path = m.group(1), m.group(2) or ""
    path = path.removeprefix("blob/") or "skill.py"
    url = _HOSTS["github"][1].format(repo=repo, path=path)
    return url if path.endswith(".py") else None


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip()).lstrip(".") or "skill.py"
    return name if name.endswith(".py") else name + ".py"


def _scan_source(code: str) -> list[str]:
    return [p for p in _DENY_PATTERNS if re.search(p, code)]


async def load_skill_file(path: Path) -> dict:
    """Import (or hot-reload) a skill module so its @tool decorators register."""
    mod_name = f"skills.{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        return {"ok": False, "error": f"cannot build import spec for {path.name}"}
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        await asyncio.to_thread(spec.loader.exec_module, module)
    except Exception as e:                                    # noqa: BLE001
        sys.modules.pop(mod_name, None)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "module": mod_name}


def installed_skills() -> list[dict]:
    out = []
    for p in sorted(SKILLS_DIR.glob("*.py")):
        out.append({"file": p.name, "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime})
    return out


# ══════════════════════════ AGENT TOOLS ══════════════════════════════════

async def _gh_raw(url: str) -> dict:
    """GET a raw.githubusercontent URL, trying main→master when HEAD 404s."""
    res = await bus.http_get(url, timeout=25)
    if res.get("status") == 404 and "/HEAD/" in url:
        for branch in ("main", "master"):
            alt = url.replace("/HEAD/", f"/{branch}/")
            res2 = await bus.http_get(alt, timeout=25)
            if res2.get("ok"):
                return res2
    return res


@tool("skill_install",
      "Install a new Python skill file from GitHub or ClawHub into the live skill "
      "directory and register its tools immediately (no restart). `source` may be a "
      "full URL (github.com/owner/repo/blob/main/path/skill.py or clawhub.ai/skills/<slug>) "
      "or shorthand 'owner/repo/path/file.py'. Returns the names of tools the skill registered.",
      {"type": "object", "properties": {
          "source": {"type": "string", "description": "GitHub or ClawHub reference / URL"},
          "name": {"type": "string",
                   "description": "Optional target filename (default: derived from source)"}},
       "required": ["source"]},
      domain="skills",
      examples=["install this skill from github.com/example/pi-weather-skill/weather.py"])
async def skill_install(source: str, name: str = "") -> dict:
    from tools.registry import all_tools
    src_key, ref = _detect_source(source)
    if not src_key:
        return {"ok": False,
                "error": "Unsupported source. Use a GitHub URL/shorthand or a ClawHub URL."}

    before = {t.name for t in all_tools()}

    if src_key == "github":
        url = _github_url(ref)
        if not url:
            return {"ok": False, "error": "Could not resolve a .py file from that GitHub reference."}
        res = await _gh_raw(url)
        if not res.get("ok"):
            # last resort: common single-file conventions at repo root
            m = re.search(r"(?:github\.com[:/]|^)([\w.-]+/[\w.-]+)", ref)
            tried_extra = False
            if m and res.get("status") == 404:
                for guess in ("skill.py", "main.py", f"{m.group(1).split('/')[1]}.py"):
                    gurl = _HOSTS["github"][1].format(repo=m.group(1), path=guess)
                    res = await _gh_raw(gurl)
                    tried_extra = True
                    if res.get("ok"):
                        url = gurl
                        break
            if not res.get("ok"):
                return {"ok": False, "error": f"Download failed: {res.get('error', res)}"
                                              + (" (also tried repo-root defaults)" if tried_extra else "")}
        code = res.get("text", "")
        fname = _safe_name(name or url.rsplit("/", 1)[-1])
    else:  # clawhub
        m = re.search(r"clawhub\.(?:ai|com)/(?:skills/)?([\w.-]+)", ref)
        if not m:
            return {"ok": False, "error": "Could not parse a ClawHub skill slug."}
        slug = m.group(1)
        if not _SLUG_RE.fullmatch(slug):
            return {"ok": False, "error": f"Invalid slug '{slug}'"}
        res = await bus.http_get(_HOSTS["clawhub"][1].format(slug=slug), timeout=25)
        if not res.get("ok"):
            return {"ok": False, "error": f"ClawHub download failed: {res.get('error', res)}"}
        code = res.get("text", "")
        fname = _safe_name(name or slug)

    if not code.strip():
        return {"ok": False, "error": "Downloaded file is empty."}
    dangerous = _scan_source(code)
    if dangerous:
        return {"ok": False, "error": f"Skill blocked by safety scan: {dangerous}"}

    path = SKILLS_DIR / fname
    backup = None
    if path.exists():                                     # atomic replace w/ rollback
        backup = path.with_suffix(".py.bak")
        shutil.copy2(path, backup)
    path.write_text(code, encoding="utf-8")

    loaded = await load_skill_file(path)
    if not loaded["ok"]:
        if backup:
            shutil.move(backup, path)
            await load_skill_file(path)
        else:
            path.unlink(missing_ok=True)
        return {"ok": False, "error": f"Skill failed to load: {loaded['error']}"}
    if backup:
        backup.unlink(missing_ok=True)

    new_tools = sorted({t.name for t in all_tools()} - before)
    await bus.emit_log(f"🧩 Skill “{fname}” installed ({len(code)} B) — new tools: "
                       f"{', '.join(new_tools) or 'none'}", kind="skills")
    await bus.emit("skills", "changed", {"action": "install", "file": fname,
                                         "new_tools": new_tools}, glow=False)
    return {"ok": True, "file": fname, "registered_tools": new_tools,
            "note": "The skill is live now — you can call its tools this session."}


@tool("skill_list",
      "List installed skill files and every tool currently registered in the harness.",
      {"type": "object", "properties": {}}, domain="skills")
async def skill_list() -> dict:
    from tools.registry import all_tools
    return {"ok": True, "dir": str(SKILLS_DIR), "skills": installed_skills(),
            "tools": [{"name": t.name, "domain": t.domain} for t in all_tools()]}


@tool("skill_reload",
      "Re-import an installed skill file (hot-patch after editing it with write_skill).",
      {"type": "object", "properties": {"file": {"type": "string"}},
       "required": ["file"]}, domain="skills")
async def skill_reload(file: str) -> dict:
    path = SKILLS_DIR / _safe_name(file)
    if not path.exists():
        return {"ok": False, "error": f"No such skill: {path.name}"}
    return await load_skill_file(path)


@tool("write_skill",
      "Create or overwrite your OWN skill file directly in the skills directory and load "
      "it immediately. Write clean async Python that imports "
      "`from tools.registry import tool` and registers functions with @tool(name, description, "
      "json_schema_parameters_dict, domain='custom'). Use this to extend yourself when no "
      "existing tool covers what the user needs.",
      {"type": "object", "properties": {
          "file": {"type": "string", "description": "filename, e.g. my_tool.py"},
          "code": {"type": "string", "description": "full python source"}},
       "required": ["file", "code"]},
      domain="skills")
async def write_skill(file: str, code: str) -> dict:
    from tools.registry import all_tools
    dangerous = _scan_source(code)
    if dangerous:
        return {"ok": False, "error": f"Blocked by safety scan: {dangerous}"}
    path = SKILLS_DIR / _safe_name(file)
    before = {t.name for t in all_tools()}
    path.write_text(code, encoding="utf-8")
    loaded = await load_skill_file(path)
    if not loaded["ok"]:
        return {"ok": False, "error": f"Syntax/import error: {loaded['error']}"}
    new = sorted({t.name for t in all_tools()} - before)
    await bus.emit_log(f"✍️ Agent authored skill “{path.name}” → tools: "
                       f"{', '.join(new) or 'none'}", kind="skills")
    return {"ok": True, "file": path.name, "registered_tools": new}


@tool("remove_skill",
      "Delete an installed skill file and unregister nothing further (its tools stop "
      "being callable after the next reload).",
      {"type": "object", "properties": {"file": {"type": "string"}},
       "required": ["file"]}, domain="skills")
async def remove_skill(file: str) -> dict:
    path = SKILLS_DIR / _safe_name(file)
    if not path.exists():
        return {"ok": False, "error": "not found"}
    path.unlink()
    sys.modules.pop(f"skills.{path.stem}", None)
    await bus.emit_log(f"🗑 Skill “{path.name}” removed", kind="skills")
    return {"ok": True, "removed": path.name}


# ══════════════════════════ BLUETOOTH (BlueZ) ════════════════════════════

async def _bt(cmd: list[str], timeout: int = 15) -> dict:
    if not shutil.which(cmd[0]):
        return {"ok": False, "error": f"'{cmd[0]}' not found — apt install bluez"}
    proc = await asyncio.create_subprocess_exec(*cmd,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": f"{' '.join(cmd)} timed out"}
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": (err or b"").decode(errors="replace").strip() or text,
                "rc": proc.returncode}
    return {"ok": True, "output": text}


@tool("bluetooth_scan",
      "Scan nearby Bluetooth devices via BlueZ (bluetoothctl). Returns device MAC/name pairs. "
      "Requires bluetooth mode 'on'.",
      {"type": "object", "properties": {
          "seconds": {"type": "integer", "description": "scan duration 3-30 s",
                      "minimum": 3, "maximum": 30}},
       "required": []},
      domain="bluetooth",
      examples=["scan for bluetooth devices for 10 seconds"])
async def bluetooth_scan(seconds: int = 8) -> dict:
    seconds = max(3, min(30, int(seconds)))
    r = await _bt(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], seconds + 8)
    if not r["ok"]:
        return r
    devs = dict(re.findall(r"^\s*\[NEW\]\s+(([0-9A-F:]{17}))\s+(.+)$",
                           r["output"], re.M | re.I))
    # fall back to paired/known devices if scan saw nothing
    if not devs:
        known = await _bt(["bluetoothctl", "devices"])
        devs = dict(re.findall(r"Device\s+([0-9A-F:]{17})\s+(.+)",
                               known.get("output", ""), re.I))
    await bus.emit_log(f"📡 BT scan → {len(devs)} device(s)", kind="bluetooth")
    return {"ok": True, "devices": devs, "seconds": seconds}


@tool("bluetooth_connect",
      "Connect to a Bluetooth device by MAC address (trust + pair fallback).",
      {"type": "object", "properties": {"mac": {"type": "string"}},
       "required": ["mac"]},
      domain="bluetooth")
async def bluetooth_connect(mac: str) -> dict:
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        return {"ok": False, "error": "MAC must look like AA:BB:CC:DD:EE:FF"}
    r = await _bt(["bluetoothctl", "connect", mac])
    if not r["ok"]:
        await _bt(["bluetoothctl", "pair", mac])
        r = await _bt(["bluetoothctl", "trust", mac])
        r2 = await _bt(["bluetoothctl", "connect", mac])
        return {"ok": r2["ok"], "attempted": "pair→trust→connect",
                "output": r2.get("output", r2.get("error", ""))}
    await bus.emit_log(f"📶 BT connected {mac}", kind="bluetooth")
    return {"ok": True, "mac": mac, "output": r["output"]}


@tool("bluetooth_disconnect",
      "Disconnect a Bluetooth device by MAC address.",
      {"type": "object", "properties": {"mac": {"type": "string"}},
       "required": ["mac"]}, domain="bluetooth")
async def bluetooth_disconnect(mac: str) -> dict:
    return await _bt(["bluetoothctl", "disconnect", mac])


@tool("bluetooth_info",
      "Bluetooth adapter status and list of paired/known devices (bluetoothctl show + devices).",
      {"type": "object", "properties": {}}, domain="bluetooth")
async def bluetooth_info() -> dict:
    show = await _bt(["bluetoothctl", "show"])
    devs = await _bt(["bluetoothctl", "devices"])
    powered = "Powered: yes" in show.get("output", "")
    return {"ok": show["ok"], "powered": powered,
            "adapter": show.get("output", show.get("error", "")),
            "devices": devs.get("output", "").splitlines()}
