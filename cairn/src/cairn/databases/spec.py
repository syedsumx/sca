"""What a managed PostgreSQL instance may be asked to be: sizes, versions, the allow-listed parameters, maintenance
windows and backup schedules. Everything a caller sends is checked here before any runtime sees it."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional


class SpecError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


NAME = re.compile(r"^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$")          # Kubernetes names: cairn-pg-<name>-<schedule>
SCHEDULE = re.compile(r"^[a-z]([a-z0-9-]{0,13}[a-z0-9])?$")
RESERVED = {"default", "verify"}
VERSIONS = ("14", "15", "16", "17")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "daily")
POOL_MODES = ("transaction", "session")
DEFAULTS = {"version": "16", "cpu": 1.0, "memory_mb": 1024, "storage_gb": 10, "replicas": 0, "pooler": None,
            "maintenance_window": None, "image": None}

_MEM = r"^\d{1,7}(kB|MB|GB)$"
_TIME_MS = r"^\d{1,9}(ms|s|min|h)?$"
# name: (kind, rule) — kind: int (min, max), float (min, max), mem (regex), time (regex), enum (values), bool.
PARAMETERS: dict[str, tuple[str, Any]] = {
    "max_connections": ("int", (20, 5000)),
    "shared_buffers": ("mem", _MEM),
    "effective_cache_size": ("mem", _MEM),
    "work_mem": ("mem", _MEM),
    "maintenance_work_mem": ("mem", _MEM),
    "autovacuum_work_mem": ("mem", r"^(-1|\d{1,7}(kB|MB|GB))$"),
    "max_wal_size": ("mem", _MEM),
    "min_wal_size": ("mem", _MEM),
    "wal_buffers": ("mem", r"^(-1|\d{1,7}(kB|MB|GB))$"),
    "temp_file_limit": ("mem", r"^(-1|\d{1,9}(kB|MB|GB))$"),
    "checkpoint_timeout": ("time", r"^\d{1,5}(s|min|h)?$"),
    "checkpoint_completion_target": ("float", (0.1, 1.0)),
    "random_page_cost": ("float", (0.1, 100.0)),
    "seq_page_cost": ("float", (0.1, 100.0)),
    "effective_io_concurrency": ("int", (0, 1000)),
    "default_statistics_target": ("int", (1, 10000)),
    "max_parallel_workers_per_gather": ("int", (0, 64)),
    "max_parallel_workers": ("int", (0, 256)),
    "max_parallel_maintenance_workers": ("int", (0, 64)),
    "max_worker_processes": ("int", (1, 256)),
    "statement_timeout": ("time", _TIME_MS),
    "lock_timeout": ("time", _TIME_MS),
    "idle_in_transaction_session_timeout": ("time", _TIME_MS),
    "idle_session_timeout": ("time", _TIME_MS),
    "log_min_duration_statement": ("time", r"^(-1|\d{1,9}(ms|s|min)?)$"),
    "log_statement": ("enum", ("none", "ddl", "mod")),
    "log_lock_waits": ("bool", None),
    "log_temp_files": ("mem", r"^(-1|\d{1,9}(kB|MB|GB)?)$"),
    "log_autovacuum_min_duration": ("time", r"^(-1|\d{1,9}(ms|s|min)?)$"),
    "track_io_timing": ("bool", None),
    "wal_compression": ("enum", ("on", "off", "pglz", "lz4", "zstd")),
    "jit": ("bool", None),
    "timezone": ("enum", ("UTC", "Etc/UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "America/New_York",
                          "America/Chicago", "America/Los_Angeles", "Asia/Tokyo", "Asia/Singapore", "Australia/Sydney")),
    "default_transaction_isolation": ("enum", ("read committed", "repeatable read", "serializable")),
    "autovacuum": ("bool", None),
    "autovacuum_max_workers": ("int", (1, 64)),
    "autovacuum_naptime": ("time", r"^\d{1,5}(s|min)?$"),
    "autovacuum_vacuum_scale_factor": ("float", (0.0, 1.0)),
    "autovacuum_analyze_scale_factor": ("float", (0.0, 1.0)),
    "autovacuum_vacuum_cost_limit": ("int", (-1, 10000)),
    "pg_stat_statements.max": ("int", (100, 100000)),
    "pg_stat_statements.track": ("enum", ("top", "all", "none")),
}


def check_name(name: str, what: str = "an instance name") -> str:
    if not isinstance(name, str) or not NAME.match(name) or name in RESERVED or name.startswith("verify-"):
        raise SpecError(f"{what} is lowercase letters, digits and '-' (up to 24), starting with a letter "
                        f"(not {', '.join(sorted(RESERVED))})")
    return name


def check_parameter(name: str, value: Any) -> str:
    """The value as PostgreSQL takes it (a string), or SpecError: only allow-listed parameters, checked values."""
    if name not in PARAMETERS:
        raise SpecError(f"{name} is not a parameter Cairn lets you set (allowed: {', '.join(sorted(PARAMETERS))})")
    kind, rule = PARAMETERS[name]
    v = value if not isinstance(value, bool) else ("on" if value else "off")
    s = str(v).strip()
    if kind == "int":
        try:
            n = int(s)
        except ValueError:
            raise SpecError(f"{name} is a whole number")
        if not rule[0] <= n <= rule[1]:
            raise SpecError(f"{name} is {rule[0]} to {rule[1]}")
        return str(n)
    if kind == "float":
        try:
            f = float(s)
        except ValueError:
            raise SpecError(f"{name} is a number")
        if not rule[0] <= f <= rule[1]:
            raise SpecError(f"{name} is {rule[0]} to {rule[1]}")
        return repr(f)
    if kind in ("mem", "time"):
        if not re.match(rule, s):
            raise SpecError(f"{name}: {s!r} is not a valid {'size (e.g. 64MB)' if kind == 'mem' else 'duration (e.g. 30s)'}")
        return s
    if kind == "enum":
        if s not in rule:
            raise SpecError(f"{name} is one of {', '.join(rule)}")
        return s
    if s.lower() in ("on", "true", "1", "yes"):
        return "on"
    if s.lower() in ("off", "false", "0", "no"):
        return "off"
    raise SpecError(f"{name} is on or off")


def check_window(w: Any) -> Optional[dict]:
    if w is None:
        return None
    if not isinstance(w, dict) or set(w) - {"day", "start", "hours"}:
        raise SpecError('maintenance_window is {"day": "sun" (or "daily"), "start": "HH:MM" (UTC), "hours": 1-8}')
    day, start, hours = w.get("day", "sun"), w.get("start", "02:00"), w.get("hours", 2)
    if day not in DAYS:
        raise SpecError(f"maintenance_window.day is one of {', '.join(DAYS)}")
    if not isinstance(start, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start):
        raise SpecError("maintenance_window.start is HH:MM (UTC)")
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 8:
        raise SpecError("maintenance_window.hours is 1 to 8")
    return {"day": day, "start": start, "hours": hours}


def in_window(w: Optional[dict], now: dt.datetime) -> bool:
    """Whether ``now`` (UTC) is inside the window (no window: always)."""
    if not w:
        return True
    h, m = (int(x) for x in w["start"].split(":"))
    for back in (0, 1):                                     # a window that started yesterday may still be open
        day = (now - dt.timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        if w["day"] != "daily" and DAYS[day.weekday()] != w["day"]:
            continue
        if day <= now < day + dt.timedelta(hours=w["hours"]):
            return True
    return False


def check_pooler(p: Any) -> Optional[dict]:
    if p is None or p is False:
        return None
    if p is True:
        p = {}
    if not isinstance(p, dict) or set(p) - {"mode", "instances", "pool_size", "max_client_connections"}:
        raise SpecError('pooler is {"mode": "transaction"|"session", "instances": 1-4, "pool_size": 1-1000, '
                        '"max_client_connections": 10-100000}')
    out = {"mode": p.get("mode", "transaction"), "instances": p.get("instances", 2), "pool_size": p.get("pool_size", 20),
           "max_client_connections": p.get("max_client_connections", 1000)}
    if out["mode"] not in POOL_MODES:
        raise SpecError("pooler.mode is transaction or session")
    for k, lo, hi in (("instances", 1, 4), ("pool_size", 1, 1000), ("max_client_connections", 10, 100000)):
        if not isinstance(out[k], int) or isinstance(out[k], bool) or not lo <= out[k] <= hi:
            raise SpecError(f"pooler.{k} is {lo} to {hi}")
    return out


def check_schedule(name: str, body: dict) -> dict:
    if not isinstance(name, str) or not SCHEDULE.match(name):
        raise SpecError("a schedule name is lowercase letters, digits and '-' (up to 15), starting with a letter")
    every, keep, verify = body.get("every_hours", 24), body.get("retention_days", 7), body.get("verify", False)
    if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 24 * 7 or (every > 24 and every % 24):
        raise SpecError("every_hours is 1 to 24, or whole days up to 168")
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 3650:
        raise SpecError("retention_days is 1 to 3650")
    if not isinstance(verify, bool):
        raise SpecError("verify is true or false")
    return {"name": name, "every_hours": every, "retention_days": keep, "verify": verify}


def check_spec(body: dict, base: Optional[dict] = None, versions: tuple[str, ...] = VERSIONS) -> dict:
    """A full instance spec from ``body`` over ``base`` (an update) or the defaults (a create)."""
    known = set(DEFAULTS)
    extra = set(body) - known - {"name"}
    if extra:
        raise SpecError(f"unknown fields: {', '.join(sorted(extra))}")
    out = dict(base or DEFAULTS)
    out.setdefault("parameters", {})
    out.setdefault("backup_schedules", [])
    for k in known:
        if k in body:
            out[k] = body[k]
    v = str(out["version"])
    if v not in versions:
        raise SpecError(f"version is one of {', '.join(versions)}")
    out["version"] = v
    try:
        cpu = float(out["cpu"])
    except (TypeError, ValueError):
        raise SpecError("cpu is a number of cores")
    if not 0.25 <= cpu <= 64:
        raise SpecError("cpu is 0.25 to 64 cores")
    out["cpu"] = cpu
    for k, lo, hi in (("memory_mb", 256, 512 * 1024), ("storage_gb", 1, 16384), ("replicas", 0, 5)):
        if not isinstance(out[k], int) or isinstance(out[k], bool) or not lo <= out[k] <= hi:
            raise SpecError(f"{k} is {lo} to {hi}")
    out["pooler"] = check_pooler(out.get("pooler"))
    out["maintenance_window"] = check_window(out.get("maintenance_window"))
    img = out.get("image")
    if img is not None and (not isinstance(img, str) or not re.match(r"^[a-z0-9][a-z0-9./_:@-]{0,254}$", img)):
        raise SpecError("image is an image reference (registry/name:tag)")
    return out


def parse_time(value: str) -> int:
    """An ISO-8601 time with a zone (2026-11-10T12:00:00Z) as epoch milliseconds."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$",
                                                  value):
        raise SpecError("a time is ISO 8601 with its zone, e.g. 2026-11-10T12:00:00Z")
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def pg_time(ms: int) -> str:
    """Epoch milliseconds as PostgreSQL's recovery_target_time takes them (UTC)."""
    t = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond:06d}+00"
