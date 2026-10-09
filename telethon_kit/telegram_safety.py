"""
Telegram User API safety layer.

Provides global rate limiting, persistent FloodWait state, daily quota
tracking, and audit logging. Designed to be additive — wraps existing
Telethon client calls without changing their signatures or return types.

Activation:
    from telegram_safety import install_safety, SafetyError
    install_safety(client)

After install_safety(client), all calls through client.send_message,
client.iter_messages, etc. go through acquire() first.

State files (in .telegram_safety/ next to this module):
    floodwait.json — { "until": iso_ts, "reason": str }
    quota.json     — { "day": "YYYY-MM-DD", "calls": {call_type: n} }
    api.log        — append-only audit log

Tunables: MIN_INTERVAL_SECONDS, DAILY_LIMITS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon.errors import FloodWaitError

log = logging.getLogger(__name__)

# ---------- Configuration ----------

MIN_INTERVAL_SECONDS = 1.0  # min gap between any two User API calls

# Daily quotas — permissive on purpose. These catch runaway loops, not
# normal usage. If you legitimately hit one, increase it.
DAILY_LIMITS: dict[str, int] = {
    "iter_dialogs": 100,
    "get_entity": 500,
    "resolve_username": 500,
    "iter_messages": 2000,
    "get_messages": 1000,
    "send_message": 2000,
    "send_file": 500,
    "edit_message": 1000,
    "delete_messages": 500,
}
DEFAULT_DAILY_LIMIT = 5000

_STATE_DIR = Path(__file__).resolve().parent / ".telegram_safety"
_FLOODWAIT_FILE = _STATE_DIR / "floodwait.json"
_QUOTA_FILE = _STATE_DIR / "quota.json"
_LOG_FILE = _STATE_DIR / "api.log"

# ---------- Exceptions ----------


class SafetyError(Exception):
    """Base class for safety-layer refusals."""


class FloodWaitActive(SafetyError):
    """Telegram earlier asked us to wait; cooldown still in effect."""


class QuotaExceeded(SafetyError):
    """Daily quota for a call type hit. Catches runaway loops."""


# ---------- State (process-local + file-backed) ----------

_lock = asyncio.Lock()
_file_lock = threading.Lock()  # for sync file writes
_last_call_monotonic = 0.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_state_dir() -> None:
    _STATE_DIR.mkdir(exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_state_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _file_lock:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)


def _today() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def _load_quota() -> dict:
    data = _read_json(_QUOTA_FILE, {"day": _today(), "calls": {}})
    if data.get("day") != _today():
        data = {"day": _today(), "calls": {}}
        _write_json(_QUOTA_FILE, data)
    return data


def _save_quota(data: dict) -> None:
    _write_json(_QUOTA_FILE, data)


def _load_floodwait() -> dict:
    return _read_json(_FLOODWAIT_FILE, {"until": None, "reason": None})


def _save_floodwait(data: dict) -> None:
    _write_json(_FLOODWAIT_FILE, data)


def _audit_log(call_type: str, extra: str = "") -> None:
    _ensure_state_dir()
    ts = _now_utc().isoformat(timespec="seconds")
    line = f"{ts}\t{call_type}\t{extra}\n"
    try:
        # Rotate if >5MB
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            _LOG_FILE.rename(_LOG_FILE.with_suffix(".log.1"))
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ---------- Public API ----------


def get_floodwait_remaining() -> float:
    """Seconds remaining on active FloodWait, or 0 if none."""
    data = _load_floodwait()
    until = data.get("until")
    if not until:
        return 0.0
    try:
        until_dt = datetime.fromisoformat(until)
    except ValueError:
        return 0.0
    remaining = (until_dt - _now_utc()).total_seconds()
    return max(0.0, remaining)


def record_floodwait(seconds: int, call_type: str) -> None:
    """Persist FloodWait deadline so it survives process restart."""
    until = _now_utc().timestamp() + seconds + 2  # +2s padding
    until_iso = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    _save_floodwait({"until": until_iso, "reason": call_type, "seconds": seconds})
    _audit_log("FLOODWAIT", f"{call_type} {seconds}s until {until_iso}")
    log.warning(
        "Telegram FloodWait recorded: %ss on %s. Persisted until %s.",
        seconds, call_type, until_iso
    )


def get_quota_status() -> dict:
    """Return {call_type: {used, limit, remaining}} + floodwait info."""
    quota = _load_quota()
    status = {}
    for call_type, used in quota["calls"].items():
        limit = DAILY_LIMITS.get(call_type, DEFAULT_DAILY_LIMIT)
        status[call_type] = {
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
        }
    return {
        "day": quota["day"],
        "calls": status,
        "floodwait_remaining_seconds": get_floodwait_remaining(),
    }


async def acquire(call_type: str) -> None:
    """
    Gate any User API call. Raises FloodWaitActive or QuotaExceeded
    if unsafe to proceed. Otherwise paces the call and records it.
    """
    global _last_call_monotonic
    async with _lock:
        # 1. Active FloodWait?
        remaining = get_floodwait_remaining()
        if remaining > 0:
            raise FloodWaitActive(
                f"Telegram FloodWait active: {remaining:.0f}s remaining. "
                f"Wait or remove .telegram_safety/floodwait.json to override."
            )

        # 2. Pace: enforce min interval
        elapsed = time.monotonic() - _last_call_monotonic
        if elapsed < MIN_INTERVAL_SECONDS:
            await asyncio.sleep(MIN_INTERVAL_SECONDS - elapsed)

        # 3. Quota
        quota = _load_quota()
        used = quota["calls"].get(call_type, 0)
        limit = DAILY_LIMITS.get(call_type, DEFAULT_DAILY_LIMIT)
        if used >= limit:
            raise QuotaExceeded(
                f"Daily quota exceeded for {call_type}: {used}/{limit}. "
                f"Resets at UTC midnight. Edit DAILY_LIMITS in telegram_safety.py if needed."
            )

        # 4. Record
        quota["calls"][call_type] = used + 1
        _save_quota(quota)
        _last_call_monotonic = time.monotonic()
        _audit_log(call_type, f"{used + 1}/{limit}")


# ---------- Wrapping helpers ----------


class _GuardedAsyncIter:
    """Wraps a Telethon async-iterable (iter_messages, iter_dialogs).

    acquire() runs once before the first __anext__, then iteration
    proceeds normally. FloodWaitError during iteration is recorded
    and re-raised so existing handlers stay in charge.
    """

    def __init__(self, inner: Any, call_type: str) -> None:
        self._inner = inner
        self._call_type = call_type
        self._acquired = False
        self._aiter = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._acquired:
            await acquire(self._call_type)
            self._aiter = self._inner.__aiter__()
            self._acquired = True
        try:
            return await self._aiter.__anext__()
        except StopAsyncIteration:
            raise
        except FloodWaitError as e:
            record_floodwait(e.seconds, self._call_type)
            raise

    # Telethon's iters also expose .total, awaitable forms, etc.
    # Delegate unknown attrs to the inner object for compatibility.
    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _wrap_coro(client_obj: Any, method_name: str, call_type: str) -> None:
    """Replace client_obj.method_name with a safety-checked version."""
    original = getattr(client_obj, method_name)

    async def wrapper(*args, **kwargs):
        await acquire(call_type)
        try:
            return await original(*args, **kwargs)
        except FloodWaitError as e:
            record_floodwait(e.seconds, call_type)
            raise

    wrapper.__wrapped__ = original
    wrapper.__name__ = f"safe_{method_name}"
    setattr(client_obj, method_name, wrapper)


def _wrap_iter(client_obj: Any, method_name: str, call_type: str) -> None:
    """Replace iter_-style sync-returning-async-iter method."""
    original = getattr(client_obj, method_name)

    def wrapper(*args, **kwargs):
        inner = original(*args, **kwargs)
        return _GuardedAsyncIter(inner, call_type)

    wrapper.__wrapped__ = original
    wrapper.__name__ = f"safe_{method_name}"
    setattr(client_obj, method_name, wrapper)


def install_safety(client_obj: Any) -> None:
    """
    Install the safety layer on a Telethon TelegramClient instance.

    Safe to call multiple times — second call is a no-op. There is no
    switch to turn it off: every client in this kit runs through it.
    """
    if getattr(client_obj, "_safety_installed", False):
        return

    # Coroutine methods
    for method, call_type in [
        ("send_message", "send_message"),
        ("send_file", "send_file"),
        ("edit_message", "edit_message"),
        ("delete_messages", "delete_messages"),
        ("get_entity", "get_entity"),
        ("get_messages", "get_messages"),
        ("get_dialogs", "iter_dialogs"),
        ("forward_messages", "send_message"),
    ]:
        if hasattr(client_obj, method):
            _wrap_coro(client_obj, method, call_type)

    # Async-iterable methods
    for method, call_type in [
        ("iter_messages", "iter_messages"),
        ("iter_dialogs", "iter_dialogs"),
        ("iter_participants", "iter_messages"),
    ]:
        if hasattr(client_obj, method):
            _wrap_iter(client_obj, method, call_type)

    client_obj._safety_installed = True
    _ensure_state_dir()
    log.info("Telegram safety layer installed on client.")


__all__ = [
    "install_safety",
    "acquire",
    "record_floodwait",
    "get_floodwait_remaining",
    "get_quota_status",
    "SafetyError",
    "FloodWaitActive",
    "QuotaExceeded",
    "MIN_INTERVAL_SECONDS",
    "DAILY_LIMITS",
]
