#!/usr/bin/env python3
"""Сторож памяти Рика: файлы памяти не переполняются.

Сам:
- «Хроника» памяти проекта (memory/projects/<Проект>.md) длиннее 25 строк — старое снизу дословно дописывается в
  archive/<Проект>-ГГГГ-ММ.md, в файле остаются 20 строк (запас, чтобы не переписывать его на каждом ходу);
  запись не делится.
- Журналы старше 30 дней — в archive/journal/ГГГГ-ММ/.
- Файл проекта правит атомарно (временный файл + os.replace) и только если он не менялся с момента чтения, иначе —
  в следующий раз. memory/NOW.md, memory/people.md, почту и разделы кроме «Хроники» не трогает.
Строками (≤ 5, «⚠ <файл>: <что> — <что сделать>»): NOW.md и people.md больше 4 КБ, файл проекта больше 8 КБ
(КБ — тысяча байт, как в Finder), NOW.md старше самого свежего файла проекта больше чем на 2 ч.

check(home=None, now=None) никогда не бросает; home — корень Рика (rick_config.home()).
CLI: mem_guard.py [--dry] — печатает строки и что сделано; --dry ничего не пишет. Нет memory/ или сбой — выход 1.
"""
import fcntl
import os
import re
import stat
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402

CHRONICLE_MAX = 25
CHRONICLE_KEEP = 20
JOURNAL_DAYS = 30
STALE_SEC = 2 * 3600
MAX_LINES = 5
HEADING = re.compile(r"#{1,6} ")
ENTRY = re.compile(r"[-*] ")
JOURNAL_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\.md$")


def check(home=None, now=None, dry=False):
    """Строки «⚠ …» (не больше 5). Механика — сама; dry=True ничего не пишет. Никогда не бросает."""
    return _run(home, now, dry)[0]


def _run(home, now, dry):
    warns, actions = [], []
    try:
        home = os.path.abspath(os.fspath(home) if home is not None else rick_config.home())
        now = now or datetime.now()
        lock = _Lock(home, dry)
        extra = []
        try:
            _trim_projects(home, now, dry, lock, actions)
            _archive_journals(home, now, dry, lock, actions, extra)
        finally:
            lock.release()
        warns = _size_warns(home) + extra
    except Exception as e:   # хуку — тишина, CLI — видно, что проверка не прошла
        actions.append("mem_guard споткнулся: %s — проверка не сделана" % e)
    return warns[:MAX_LINES], actions


class _Lock:
    """Один mem_guard за раз: state/.mem_guard.lock, берётся только когда есть что двигать."""

    def __init__(self, home, dry):
        self.path = os.path.join(home, "state", ".mem_guard.lock")
        self.dry, self.fd, self.busy = dry, None, False

    def take(self, actions):
        if self.dry or self.fd is not None:
            return True
        if self.busy:
            return False
        fd = None
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
            return True
        except OSError:
            if fd is not None:
                os.close(fd)
            self.busy = True
            actions.append("mem_guard уже работает — перенос в следующий раз")
            return False

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# --- «Хроника» памяти проектов --------------------------------------------------------------------------------

def _trim_projects(home, now, dry, lock, actions):
    ddir = os.path.join(home, "memory", "projects")
    try:
        names = sorted(os.listdir(ddir))
    except OSError:
        return
    for name in names:
        path = os.path.join(ddir, name)
        if name.startswith((".", "_")) or not name.endswith(".md") or os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            _trim_one(path, name, os.path.join(home, "archive"), now, dry, lock, actions)
        except Exception:
            continue


def _split(text):
    """Хроника → (строки до записей, записи, строки после). Запись — строка «- …» и её продолжения. Нет раздела — None."""
    lines = text.splitlines(True)
    start = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("## Хроника")), None)
    if start is None:
        return None
    end = next((j for j in range(start, len(lines)) if HEADING.match(lines[j])), len(lines))
    body_end = end
    while body_end > start and not lines[body_end - 1].strip():
        body_end -= 1
    head, entries = lines[:start], []
    for ln in lines[start:body_end]:
        if ENTRY.match(ln):
            entries.append([ln])
        elif entries:
            entries[-1].append(ln)
        else:
            head.append(ln)
    return head, entries, lines[body_end:]


def _count(lines):
    return sum(1 for ln in lines if ln.strip())


def _plan_trim(text):
    """(новый текст, перенесённые строки, строк в хронике) или None, если хроника не длиннее 25 строк."""
    parts = _split(text)
    if parts is None:
        return None
    head, entries, tail = parts
    total = _count(ln for e in entries for ln in e)
    if total <= CHRONICLE_MAX:
        return None
    keep, used = 0, 0
    for e in entries:
        n = _count(e)
        if keep and used + n > CHRONICLE_KEEP:
            break
        keep, used = keep + 1, used + n
    kept = [ln for e in entries[:keep] for ln in e]
    moved = [ln for e in entries[keep:] for ln in e]
    if not moved:
        return None
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    return "".join(head + kept + tail), moved, total


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _trim_one(path, name, adir, now, dry, lock, actions):
    data = _read_bytes(path)
    plan = _plan_trim(data.decode("utf-8"))
    if plan is None:
        return
    new_text, moved, total = plan
    proj = name[:-3]
    arch_name = "%s-%s.md" % (proj, now.strftime("%Y-%m"))
    what = "memory/projects/%s: «Хроника» %d строк — %d старых" % (name, total, _count(moved))
    if dry:
        actions.append("(dry) %s перенёс бы в archive/%s" % (what, arch_name))
        return
    if not lock.take(actions):
        return
    os.makedirs(adir, exist_ok=True)
    arch = os.path.join(adir, arch_name)
    block = "\n## %s · из «Хроники» memory/projects/%s\n%s" % (now.strftime("%d.%m.%Y %H:%M"), name, "".join(moved))
    if not block.endswith("\n"):
        block += "\n"
    tmp = _temp_copy(path, new_text.encode("utf-8"))
    created, old_size, done = not os.path.exists(arch), None, False
    try:
        with open(arch, "ab") as f:
            old_size = f.seek(0, os.SEEK_END)
            if created:
                f.write(("# %s · архив хроники %s\nПереносит tools/mem_guard.py из memory/projects/%s дословно; "
                         "блоки дописываются снизу, в блоке новое сверху.\n" % (proj, now.strftime("%Y-%m"), name))
                        .encode("utf-8"))
            f.write(block.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        if _read_bytes(path) == data:
            os.replace(tmp, path)
            done = True
            actions.append("%s перенесены в archive/%s" % (what, arch_name))
        else:
            actions.append("memory/projects/%s: менялся во время правки — хроника не тронута, в следующий раз" % name)
    finally:
        if not done:
            _unlink(tmp)
            if old_size is not None:
                if created:
                    _unlink(arch)
                else:
                    os.truncate(arch, old_size)


def _temp_copy(path, payload):
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
    except Exception:
        _unlink(tmp)
        raise
    return tmp


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --- Журналы ---------------------------------------------------------------------------------------------------

def _archive_journals(home, now, dry, lock, actions, warns):
    jdir = os.path.join(home, "journal")
    try:
        names = sorted(os.listdir(jdir))
    except OSError:
        return
    today = now.date()
    for name in names:
        try:
            m = JOURNAL_NAME.match(name)
            if not m:
                continue
            day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            src = os.path.join(jdir, name)
            if (today - day).days <= JOURNAL_DAYS or os.path.islink(src) or not os.path.isfile(src):
                continue
            sub = "%04d-%02d" % (day.year, day.month)
            dst = os.path.join(home, "archive", "journal", sub, name)
            if os.path.exists(dst):
                warns.append("⚠ journal/%s: в archive/journal/%s/ уже есть файл с этим именем — сверить и оставить "
                             "один" % (name, sub))
                continue
            if dry:
                actions.append("(dry) journal/%s → archive/journal/%s/" % (name, sub))
                continue
            if not lock.take(actions):
                return
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            actions.append("journal/%s → archive/journal/%s/" % (name, sub))
        except Exception:
            continue


# --- Потолки ---------------------------------------------------------------------------------------------------

def _stat(path):
    try:
        st = os.stat(path)
        return st if stat.S_ISREG(st.st_mode) else None
    except OSError:
        return None


def _kb(size, limit_kb):
    text = "%.1f" % (size / 1000.0)
    if float(text) <= limit_kb:
        text = "%.2f" % (size / 1000.0)
    return text.replace(".", ",")


def _over(rel, st, limit_kb, todo):
    if st is None or st.st_size <= limit_kb * 1000:
        return None
    return "⚠ %s: %s КБ при потолке %d — %s" % (rel, _kb(st.st_size, limit_kb), limit_kb, todo)


def _size_warns(home):
    mdir = os.path.join(home, "memory")
    pdir = os.path.join(mdir, "projects")
    st_now = _stat(os.path.join(mdir, "NOW.md"))
    projects = []
    try:
        for name in sorted(os.listdir(pdir)):
            if name.endswith(".md") and not name.startswith((".", "_")):
                st = _stat(os.path.join(pdir, name))
                if st is not None:
                    projects.append((name, st))
    except OSError:
        pass
    out = [_over("memory/NOW.md", st_now, 4, "сжать: по строке на проект, подробности — в memory/projects/")]
    if st_now is not None and projects:
        name, st = max(projects, key=lambda d: d[1].st_mtime)
        lag = st.st_mtime - st_now.st_mtime
        if lag > STALE_SEC:
            out.append("⚠ memory/NOW.md: старше memory/projects/%s на %d ч %02d мин — пересобрать из файлов проектов"
                       % (name, lag // 3600, lag % 3600 // 60))
    out.append(_over("memory/people.md", _stat(os.path.join(mdir, "people.md")), 4, "сжать: роли без подробностей"))
    for name, st in sorted(projects, key=lambda d: -d[1].st_size):
        out.append(_over("memory/projects/" + name, st, 8, "сжать «Сейчас» и «Решения», старое — в archive/"))
    return [line for line in out if line]


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if any(a != "--dry" for a in args):
        print("использование: mem_guard.py [--dry]", file=sys.stderr)
        return 2
    mem = os.path.join(rick_config.home(), "memory")
    if not os.path.isdir(mem):   # пустой вывод значил бы «всё в порядке», а смотреть было нечего
        print("⚠ папки памяти %s нет — ничего не проверено (RICK_HOME?)" % mem, file=sys.stderr)
        return 1
    warns, actions = _run(None, None, "--dry" in args)
    for line in warns + ["· " + a for a in actions]:
        print(line)
    return 1 if any(a.startswith("mem_guard споткнулся") for a in actions) else 0


if __name__ == "__main__":
    sys.exit(main())
