#!/usr/bin/env python3
"""Строка состояния Рика внизу терминала: «🔰 Рик · 187K · кэш 96% · 3 чата · 📥 2 · 2 задачи».

Claude Code зовёт её сам (statusLine в .claude/settings.json) и подаёт на stdin JSON: transcript_path, model,
workspace, context_window. Ни одного токена: только файлы и `tmux ls`.
  контекст и кэш — context_window.current_usage, нет его — последний usage в хвосте транскрипта;
  чаты — живые сессии rick-* (`tmux ls`, таймаут 0,3 с); почта — inbox/queue.jsonl минус delivered.jsonl;
  задачи — memory/tasks.jsonl (open и doing). Напоминания живут в «Напоминаниях» Мака (remind.py) — их здесь нет.
Нулевое не показывается. Любая ошибка — пустая строка и код 0: строка состояния не должна мешать работе.
Подмены для тестов: RICK_HOME, RICK_TMUX.
"""
import json
import os
import shutil
import subprocess
import sys

HOME = os.environ.get("RICK_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAIL = 512 * 1024


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def usage_numbers(usage):
    """(контекст в токенах, доля кэша в %) по usage модели."""
    fresh = int(usage.get("input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    made = int(usage.get("cache_creation_input_tokens") or 0)
    total = fresh + read + made
    return total, (round(100 * read / total) if total else None)


def last_usage(path):
    """Последний usage главной ветки в хвосте транскрипта."""
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - TAIL))
        lines = fh.read().decode("utf-8", "ignore").splitlines()
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("type") == "assistant" and not rec.get("isSidechain"):
                usage = rec["message"]["usage"]
                if isinstance(usage, dict):
                    return usage
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def context(data):
    window = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    usage = window.get("current_usage")
    if not isinstance(usage, dict) and data.get("transcript_path"):
        try:
            usage = last_usage(data["transcript_path"])
        except OSError:
            usage = None
    return usage_numbers(usage) if isinstance(usage, dict) else (None, None)


def chats():
    exe = os.environ.get("RICK_TMUX") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    try:
        out = subprocess.run([exe, "ls", "-F", "#{session_name}"], capture_output=True, text=True,
                             timeout=0.3).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for name in out.splitlines() if name.startswith("rick-"))


def jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def mail():
    done = {r.get("id") for r in jsonl(os.path.join(HOME, "inbox", "delivered.jsonl"))}
    return sum(1 for r in jsonl(os.path.join(HOME, "inbox", "queue.jsonl")) if r.get("id") not in done)


def tasks():
    last = {}
    for row in jsonl(os.path.join(HOME, "memory", "tasks.jsonl")):
        if "id" in row:
            last[row["id"]] = row
    return sum(1 for row in last.values() if row.get("status") in ("open", "doing"))


def line(data):
    parts = ["🔰 Рик"]
    ctx, cache = context(data)
    if ctx:
        parts.append("%dK" % round(ctx / 1000))
    if cache is not None:
        parts.append("кэш %d%%" % cache)
    n = chats()
    if n:
        parts.append("%d %s" % (n, plural(n, "чат", "чата", "чатов")))
    n = mail()
    if n:
        parts.append("📥 %d" % n)
    n = tasks()
    if n:
        parts.append("%d %s" % (n, plural(n, "задача", "задачи", "задач")))
    return " · ".join(parts)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        print(line(data if isinstance(data, dict) else {}))
    except Exception:  # noqa: BLE001 — строка состояния молчит, а не падает
        print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
