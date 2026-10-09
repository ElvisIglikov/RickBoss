#!/usr/bin/env python3
"""Уведомление Мака: баннер в углу экрана и системный звук — когда долгое кончилось и нужен хозяин.

  python3 tools/notify.py "<текст>" [--sound Glass] [--title Рик]

Звук — из /System/Library/Sounds (Glass, Ping, Pop, Hero…). Включается в config.json:
"notify": {"sound": true, "banner": true}. Подмены для тестов — $RICK_OSASCRIPT, $RICK_AFPLAY.
"""
import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402

SOUNDS = "/System/Library/Sounds"
# текст и заголовок — аргументами, не вставкой в код: кавычки в тексте скрипт не ломают
SCRIPT = "on run argv\n  display notification (item 1 of argv) with title (item 2 of argv)\nend run\n"


def settings():
    cfg = rick_config.load().get("notify")
    return dict({"sound": True, "banner": True}, **(cfg if isinstance(cfg, dict) else {}))


def sound_file(name):
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,40}", name or ""):
        raise SystemExit("Звук называется словом из %s, например Glass." % SOUNDS)
    path = os.path.join(SOUNDS, name + ".aiff")
    if not os.path.exists(path):
        have = sorted(f[:-5] for f in os.listdir(SOUNDS) if f.endswith(".aiff")) if os.path.isdir(SOUNDS) else []
        raise SystemExit("Звука %s нет%s." % (name, "; есть: " + ", ".join(have) if have else " — это не Мак"))
    return path


def _run(cmd, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, **kw)
    except FileNotFoundError:
        raise SystemExit("Нет %s — уведомления Рика работают только на macOS." % os.path.basename(cmd[0]))
    except subprocess.TimeoutExpired:
        raise SystemExit("%s не ответил за 20 с." % os.path.basename(cmd[0]))
    if r.returncode != 0:
        raise SystemExit("%s: %s" % (os.path.basename(cmd[0]), (r.stderr.strip().splitlines() or ["ошибка"])[-1]))


def notify(text, sound="Glass", title="Рик"):
    text, cfg, done = " ".join(str(text).split())[:300], settings(), []
    if not text:
        raise SystemExit("Пустое уведомление — нечего показывать.")
    if cfg.get("banner"):
        _run([os.environ.get("RICK_OSASCRIPT") or "osascript", "-", text, title or "Рик"], input=SCRIPT)
        done.append("баннер")
    if cfg.get("sound") and sound:
        _run([os.environ.get("RICK_AFPLAY") or "afplay", sound_file(sound)])
        done.append("звук " + sound)
    return ("Уведомил: %s." % " и ".join(done)) if done else "Уведомления выключены в config.json (notify)."


def main(argv=None):
    ap = argparse.ArgumentParser(description="Уведомление Мака: баннер и звук")
    ap.add_argument("text")
    ap.add_argument("--sound", default="Glass", help="имя звука из %s; пусто — без звука" % SOUNDS)
    ap.add_argument("--title", default="Рик")
    a = ap.parse_args(argv)
    print(notify(a.text, a.sound, a.title))
    return 0


if __name__ == "__main__":
    sys.exit(main())
