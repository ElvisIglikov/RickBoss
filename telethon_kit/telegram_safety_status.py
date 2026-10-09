#!/usr/bin/env python3
"""Show current Telegram User API safety state.

Usage:
    python3 telegram_safety_status.py          # show today's quota + floodwait
    python3 telegram_safety_status.py --reset  # clear quota counter (use carefully)
    python3 telegram_safety_status.py --clear-floodwait  # cancel persisted cooldown
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from telegram_safety import (  # noqa: E402
    DAILY_LIMITS,
    DEFAULT_DAILY_LIMIT,
    _FLOODWAIT_FILE,
    _QUOTA_FILE,
    get_quota_status,
)


def show_status():
    s = get_quota_status()
    print(f"Day (UTC): {s['day']}")
    fw = s["floodwait_remaining_seconds"]
    if fw > 0:
        print(f"FloodWait: ACTIVE, {fw:.0f}s remaining")
    else:
        print("FloodWait: clear")
    print()

    if not s["calls"]:
        print("No API calls recorded today.")
        return

    print(f"{'call_type':<22} {'used':>6} {'limit':>6} {'remaining':>10}")
    print("-" * 50)
    for call_type, info in sorted(s["calls"].items(), key=lambda x: -x[1]["used"]):
        bar = ""
        pct = info["used"] / info["limit"] if info["limit"] else 0
        if pct >= 0.9:
            bar = "  CRITICAL"
        elif pct >= 0.7:
            bar = "  warning"
        print(
            f"{call_type:<22} {info['used']:>6} {info['limit']:>6} "
            f"{info['remaining']:>10}{bar}"
        )
    print()
    print("Limits live in telegram_safety.py -> DAILY_LIMITS.")


def reset_quota():
    if _QUOTA_FILE.exists():
        _QUOTA_FILE.unlink()
        print(f"Removed {_QUOTA_FILE}. Counter resets on next API call.")
    else:
        print("No quota file to remove.")


def clear_floodwait():
    if _FLOODWAIT_FILE.exists():
        _FLOODWAIT_FILE.unlink()
        print(f"Removed {_FLOODWAIT_FILE}. WARNING: this overrides Telegram's request to wait.")
    else:
        print("No floodwait file to remove.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="clear today's quota counter")
    p.add_argument("--clear-floodwait", action="store_true", help="cancel active cooldown")
    args = p.parse_args()

    if args.reset:
        reset_quota()
        return
    if args.clear_floodwait:
        clear_floodwait()
        return
    show_status()


if __name__ == "__main__":
    main()
