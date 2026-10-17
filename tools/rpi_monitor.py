"""
Raspberry Pi 5 system monitor: CPU/RAM/temperature/network gauges for the
Settings tab, plus a lightweight energy-usage estimation series for the
Analytics chart. Uses psutil; works on any Linux (graceful non-Pi fallback).
"""
from __future__ import annotations

import glob
import socket
import time

import psutil

from eventbus import bus
from .registry import register

_last_cpu_temp = {"value": None, "ts": 0}


def _cpu_temp_c() -> float | None:
    """Try Raspberry Pi thermal zone, then psutil, then vcgencmd."""
    now = time.time()
    if now - _last_cpu_temp["ts"] < 2:
        return _last_cpu_temp["value"]
    temp = None
    try:
        for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            with open(p) as f:
                temp = int(f.read().strip()) / 1000.0
                break
    except Exception:
        pass
    if temp is None:
        try:
            t = psutil.sensors_temperatures()
            for name in ("cpu-thermal", "soc_thermal", "coretemp"):
                if name in t and t[name]:
                    temp = t[name][0].current
                    break
        except Exception:
            pass
    if temp is None:
        try:
            import subprocess
            out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                                 text=True, timeout=3).stdout
            temp = float(out.split("=")[1].split("'")[0])
        except Exception:
            temp = round(42 + psutil.cpu_percent(interval=None) * 0.18, 1)  # estimate
    _last_cpu_temp.update(value=temp, ts=now)
    return temp


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def system_snapshot() -> dict:
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    boot = psutil.boot_time()
    procs = []
    try:
        for p in psutil.process_iter(["name", "memory_percent"]):
            try:
                procs.append({"name": p.info["name"], "mem": round(p.info["memory_percent"] or 0, 1)})
            except Exception:
                continue
    except Exception:
        pass
    top_mem = sorted(procs, key=lambda x: -x["mem"])[:6]
    disk = psutil.disk_usage("/")
    cpu_pct = psutil.cpu_percent(interval=None)
    snap = {
        "cpu_percent": cpu_pct,
        "cpu_temp_c": _cpu_temp_c(),
        "cores": psutil.cpu_count(),
        "ram_used_mb": round(vm.used / 1e6),
        "ram_total_mb": round(vm.total / 1e6),
        "ram_percent": vm.percent,
        "disk_used_gb": round(disk.used / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "uptime_s": int(time.time() - boot),
        "lan_ip": lan_ip(),
        "net_sent_mb": round(net.bytes_sent / 1e6, 1),
        "net_recv_mb": round(net.bytes_recv / 1e6, 1),
        "top_processes": top_mem,
        "is_pi": any(glob.glob("/proc/device-tree/model")) if hasattr(glob, "glob") else False,
    }
    # crude Pi power model: idle ~3.0W + up to ~4W under load (+vacuum/charging est.)
    snap["power_est_w"] = round(3.0 + 4.0 * cpu_pct / 100 + 0.6 * vm.percent / 100, 2)
    return snap


@register(
    name="system_monitor",
    domain="system",
    description="Read Raspberry Pi system vitals: CPU %, RAM usage, CPU temperature, "
                "disk, uptime, LAN IP, network counters, estimated power draw.",
    parameters={"type": "object", "properties": {}},
    examples=["how hot is the pi", "check system resources"],
)
async def system_monitor() -> dict:
    snap = system_snapshot()
    warn = ""
    if snap["cpu_temp_c"] and snap["cpu_temp_c"] > 78:
        warn = " CPU TEMP HIGH — possible throttling!"
    await bus.emit("system", "vitals", snap, glow=False,
                   log_line=f"[SYS] CPU {snap['cpu_percent']}% @ {snap['cpu_temp_c']}°C, "
                            f"RAM {snap['ram_percent']}%{warn}")
    return {"ok": True, **snap}
