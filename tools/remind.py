#!/usr/bin/env python3
"""Напоминание в приложение «Напоминания» Мака: у Рика своих таймеров нет, напоминает Мак.

  python3 tools/remind.py "<текст>" --at "ГГГГ-ММ-ДД ЧЧ:ММ"

Время — ровно названное хозяином: «завтра в 10» — завтрашняя дата и 10:00, а не «сейчас + сутки».
Прошедшее время и кривая дата — отказ с объяснением. Подмена osascript для тестов — $RICK_OSASCRIPT.
"""
import argparse
import datetime
import os
import subprocess
import sys

DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# Текст и дата — аргументами, не вставкой в код: кавычки в тексте скрипт не ломают.
SCRIPT = """on run argv
  set d to current date
  set day of d to 1
  set year of d to (item 2 of argv) as integer
  set month of d to (item 3 of argv) as integer
  set day of d to (item 4 of argv) as integer
  set time of d to (item 5 of argv) as integer
  tell application "Reminders" to make new reminder with properties {name:(item 1 of argv), remind me date:d}
  return "ok"
end run
"""
DENIED = ("-1743", "not authorized", "not allowed", "не разрешено")


def parse_at(text, now=None):
    now = now or datetime.datetime.now()
    try:
        at = datetime.datetime.strptime(" ".join(str(text).split()), "%Y-%m-%d %H:%M")
    except ValueError:
        raise SystemExit("Время не разобрал: нужно «ГГГГ-ММ-ДД ЧЧ:ММ», например «%s 10:00»."
                         % (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    if at <= now:
        raise SystemExit("Время %s уже прошло — напоминание не поставил." % at.strftime("%d.%m %H:%M"))
    return at


def human(at):
    return "%s %s в %s" % (DAYS[at.weekday()], at.strftime("%d.%m"), at.strftime("%H:%M"))


def remind(text, at):
    text = " ".join(str(text).split())
    if not text:
        raise SystemExit("Пустое напоминание — нечего ставить.")
    exe = os.environ.get("RICK_OSASCRIPT") or "osascript"
    args = [text[:300], str(at.year), str(at.month), str(at.day), str(at.hour * 3600 + at.minute * 60)]
    try:
        r = subprocess.run([exe, "-", *args], input=SCRIPT, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise SystemExit("Напоминаний Мака здесь нет (нет osascript) — Рик работает только на macOS.")
    except subprocess.TimeoutExpired:
        raise SystemExit("Мак ждёт разрешения на «Напоминания»: нажми «Разрешить» в окне Мака и повтори.")
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        if any(x in err.lower() for x in DENIED):
            raise SystemExit("Мак не дал доступ к «Напоминаниям»: Системные настройки → Конфиденциальность и "
                             "безопасность → Автоматизация → включи «Напоминания» у Терминала, потом повтори.")
        raise SystemExit("«Напоминания» не приняли напоминание: %s" % (err.splitlines() or ["без текста ошибки"])[-1])
    return "Поставил в «Напоминания» Мака: «%s» — %s. Напомнит Мак, а не Рик." % (text[:300], human(at))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Напоминание в «Напоминания» Мака")
    ap.add_argument("text", help="что напомнить")
    ap.add_argument("--at", required=True, help="ГГГГ-ММ-ДД ЧЧ:ММ")
    a = ap.parse_args(argv)
    print(remind(a.text, parse_at(a.at)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
