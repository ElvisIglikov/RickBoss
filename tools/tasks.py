#!/usr/bin/env python3
"""Задачи Рика с номерами #N: каждое слово хозяина — задача, ничего не теряется. Без серверов, одним файлом.

  tasks.py add "заголовок" [--project P] [--original "дословно"] [--summary "суть"] [--start]
  tasks.py add-json -        — пачка одним вызовом: [{"title", "project", "original", "summary", "start"}]
  tasks.py list [--project P] [--open] [--limit 15]
  tasks.py show N | start N | done N [--result "что сделано"] | cancel N --reason "почему"

Задачи не удаляются: лишняя — cancel N --reason "дубль #M". Файл memory/tasks.jsonl: по строке на запись
{"id", "title", "project", "original", "summary", "status": "open|doing|done|cancelled", "result", "created",
"updated"}, последняя запись с этим id побеждает; пишется под fcntl. Файла нет, пока не было add.
"""
import argparse
import datetime
import fcntl
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_bus  # noqa: E402
import rick_config  # noqa: E402

MARK = {"open": "⬜", "doing": "💭", "done": "✅", "cancelled": "✖"}
FIELDS = ("title", "project", "original", "summary")


class TaskError(Exception):
    """Отказ с понятной причиной: печатается и выходит кодом 2."""


def path():
    return os.path.join(rick_config.home(), "memory", "tasks.jsonl")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse(text):
    tasks = {}
    for line in text.splitlines():
        try:
            rec = json.loads(line)
            tasks[int(rec["id"])] = rec
        except (ValueError, KeyError, TypeError):
            continue   # битая строка (оборванная запись) — не роняет весь список
    return tasks


def load():
    """{id: последняя запись}. Файла нет — пусто, и файл не создаётся."""
    try:
        with open(path(), encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            return _parse(fh.read())
    except FileNotFoundError:
        return {}


def _write(change):
    """change(tasks) -> новые записи; читает и дописывает под одной блокировкой — номера не повторяются."""
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    with open(path(), "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        rows = change(_parse(data := fh.read()))
        fh.write("\n" * bool(data and not data.endswith("\n")) + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        fh.flush()
        os.fsync(fh.fileno())
    return rows


def _text(value):
    return rick_bus.clean(" ".join(str(value or "").split()))   # телефоны и ключи в задачи не попадают


def add(items):
    """items — [{"title", "project", "original", "summary", "start"}]; вернёт новые записи с номерами."""
    if not isinstance(items, list) or not items:
        raise TaskError("нужен непустой список задач")
    for it in items:
        if not isinstance(it, dict) or not _text(it.get("title")):
            raise TaskError("у задачи нет заголовка: %s" % json.dumps(it, ensure_ascii=False)[:80])

    def change(tasks):
        n, t, rows = max(tasks, default=0), _now(), []
        for it in items:
            n += 1
            rows.append(dict({f: _text(it.get(f)) for f in FIELDS}, id=n, result="", created=t, updated=t,
                             status="doing" if it.get("start") else "open"))
        return rows
    return _write(change)


def update(n, status, result=None):
    if n not in load():   # до _write: без задач файл памяти не появляется
        raise TaskError("задачи #%d нет" % n)

    def change(tasks):
        if n not in tasks:
            raise TaskError("задачи #%d нет" % n)
        old = tasks[n]
        if old["status"] in ("done", "cancelled"):
            raise TaskError("#%d уже закрыта (%s) — новое дело заведи новой задачей" % (n, old["status"]))
        return [dict(old, status=status, updated=_now(), **({"result": _text(result)} if result is not None else {}))]
    return _write(change)[0]


def line(t):
    upd = t.get("updated", "")[5:16].replace("T", " ")
    upd = "%s.%s %s" % (upd[3:5], upd[0:2], upd[6:]) if len(upd) == 11 else upd
    return "#%d %s %s%s · %s" % (t["id"], MARK.get(t["status"], "?"), t["title"],
                                  " · " + t["project"] if t.get("project") else "", upd)


def listing(project=None, only_open=False, limit=15):
    rows = [t for t in load().values() if (not project or t.get("project", "").casefold() == project.casefold())
            and (not only_open or t["status"] in ("open", "doing"))]
    return sorted(rows, key=lambda t: (t.get("updated", ""), t["id"]), reverse=True)[:limit]


def brief(limit=5):
    """Для хука start: «задач открыто N» и свежие открытые; задач нет — пустая строка (контекст не засоряем)."""
    rows = listing(only_open=True, limit=10 ** 6)
    return "\n".join(["задач открыто %d (python3 tools/tasks.py list --open)" % len(rows)] +
                     [line(t) for t in rows[:limit]]) if rows else ""


def show(n):
    t = load().get(n)
    if not t:
        raise TaskError("задачи #%d нет" % n)
    return "\n".join([line(t)] + ["%s: %s" % (k, t[k]) for k in ("original", "summary", "result", "created")
                                  if t.get(k)])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tasks.py", description="Задачи Рика с номерами #N.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("title")
    for o in ("--project", "--original", "--summary"):
        p.add_argument(o, default="")
    p.add_argument("--start", action="store_true")
    sub.add_parser("add-json").add_argument("source", help="- (stdin) или файл с JSON-массивом")
    p = sub.add_parser("list")
    p.add_argument("--project")
    p.add_argument("--open", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    for cmd in ("show", "start", "done", "cancel"):
        p = sub.add_parser(cmd)
        p.add_argument("n", type=lambda s: int(s.lstrip("#")))
        if cmd == "done":
            p.add_argument("--result")
        if cmd == "cancel":
            p.add_argument("--reason", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "add":
            rows = add([{"title": a.title, "project": a.project, "original": a.original, "summary": a.summary,
                         "start": a.start}])
        elif a.cmd == "add-json":
            try:
                rows = add(json.loads(sys.stdin.read() if a.source == "-" else open(a.source, encoding="utf-8").read()))
            except ValueError as e:
                raise TaskError("не JSON: %s" % e)
        elif a.cmd == "list":
            rows = listing(a.project, a.open, a.limit)
            return print("\n".join(map(line, rows)) if rows else "задач нет") or 0
        elif a.cmd == "show":
            return print(show(a.n)) or 0
        else:
            st = {"start": "doing", "done": "done", "cancel": "cancelled"}[a.cmd]
            rows = [update(a.n, st, a.result if a.cmd == "done" else getattr(a, "reason", None))]
        return print("\n".join(map(line, rows))) or 0
    except (TaskError, OSError) as e:
        return print("tasks.py: %s" % e, file=sys.stderr) or 2


if __name__ == "__main__":
    sys.exit(main())
