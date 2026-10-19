"""Machine and power-state capture for benchmark results, and the comparability rule between two captures.

`env_class` is the key two result files must share before their numbers are compared: CPU brand, logical core
count, memory, power source and Low Power Mode. Load average and thermal state are recorded per run but are
advisory (flagged, not blocking).
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys

__all__ = ["capture", "comparable", "load1", "power_state"]


def _cmd(args: list[str]) -> str | None:
    if shutil.which(args[0]) is None:
        return None
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _sysctl(name: str) -> str | None:
    return _cmd(["sysctl", "-n", name])


def load1() -> float:
    """1-minute load average (0.0 where unsupported)."""
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def power_state() -> dict:
    """{power_source: ac|battery|unknown, low_power_mode: bool|None, thermal_warning: bool|None} (macOS pmset)."""
    if sys.platform != "darwin":
        return {"power_source": "unknown", "low_power_mode": None, "thermal_warning": None}
    batt = _cmd(["pmset", "-g", "batt"]) or ""
    src = "ac" if "AC Power" in batt else ("battery" if "Battery Power" in batt else "unknown")
    g = _cmd(["pmset", "-g"]) or ""
    m = re.search(r"lowpowermode\s+(\d)", g)
    therm = _cmd(["pmset", "-g", "therm"]) or ""
    warn = None if not therm else not ("No thermal warning" in therm and "No performance warning" in therm)
    return {"power_source": src, "low_power_mode": (m.group(1) == "1") if m else None, "thermal_warning": warn}


def capture(extra: dict | None = None) -> dict:
    """Snapshot of the machine, toolchain and power state."""
    darwin = sys.platform == "darwin"
    cpu = (_sysctl("machdep.cpu.brand_string") if darwin else None) or platform.processor() or "unknown"
    ncpu = os.cpu_count() or 0
    mem = int(_sysctl("hw.memsize") or 0) if darwin else 0
    if not darwin and os.path.exists("/proc/meminfo"):
        with open("/proc/meminfo") as f:
            m = re.search(r"MemTotal:\s+(\d+)", f.read())
            mem = int(m.group(1)) * 1024 if m else 0
    env = {
        "machine": platform.machine(),
        "os": (f"macOS {platform.mac_ver()[0]} ({_sysctl('kern.osversion')})" if darwin else platform.platform()),
        "cpu_brand": cpu,
        "ncpu": ncpu,
        "perf_cores": int(_sysctl("hw.perflevel0.physicalcpu") or 0) if darwin else None,
        "eff_cores": int(_sysctl("hw.perflevel1.physicalcpu") or 0) if darwin else None,
        "mem_bytes": mem,
        "python": sys.version.split()[0],
        "rustc": _cmd(["rustc", "--version"]),
        "load_avg_start": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        **power_state(),
    }
    env["env_class"] = env_class(env)
    if extra:
        env.update(extra)
    return env


def env_class(env: dict) -> str:
    lpm = env.get("low_power_mode")
    return "|".join(
        [
            str(env.get("cpu_brand")),
            f"{env.get('ncpu')}c",
            f"{round((env.get('mem_bytes') or 0) / 2**30)}G",
            str(env.get("power_source")),
            "lpm1" if lpm else ("lpm0" if lpm is False else "lpm?"),
        ]
    )


def comparable(base: dict, cur: dict) -> list[str]:
    """Reasons the two environments are not comparable (empty list = comparable)."""
    reasons = []
    for k in ("machine", "cpu_brand", "ncpu", "mem_bytes"):
        if base.get(k) != cur.get(k):
            reasons.append(f"{k}: baseline {base.get(k)!r} vs current {cur.get(k)!r}")
    for k in ("power_source", "low_power_mode"):
        if base.get(k) != cur.get(k):
            reasons.append(f"{k}: baseline {base.get(k)!r} vs current {cur.get(k)!r} (frequency policy differs)")
    return reasons
