"""Тесты tools/mem_guard.py. Работают во временной папке с выдуманной памятью; настоящую папку Рика не пишут."""
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)
import mem_guard  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0)
LINE = re.compile(r"^⚠ [^:]+: .+ — .+$")


def snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for d in dirs:
            out[os.path.relpath(os.path.join(base, d), root) + "/"] = None
        for f in files:
            p = os.path.join(base, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = (fh.read(), st.st_mtime_ns)
    return out


def chronicle(text):
    """Строки записей хроники (без пустых), как видит человек."""
    parts = mem_guard._split(text)
    return [ln for e in parts[1] for ln in e if ln.strip()]


SAMPLE = ("# Site · сайт-визитка\nОбновлено 01.10\n\n## Сейчас\n- верстаю лендинг\n\n## Хроника (новое сверху)\n"
          "- 01.10 09:00 · [агент] Сайт · шаг с продолжением\n  ⚠ исправлено: шаг был не тот\n"
          + "".join("- 30.09 %02d:00 · [агент] Сайт · шаг %d\n" % (i % 24, i) for i in range(10))
          + "\n## Решения\n- без трекеров\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mem_guard_test_")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dd = os.path.join(self.home, "memory", "projects")
        os.makedirs(self.dd)

    def read(self, *parts):
        with open(os.path.join(self.home, *parts), encoding="utf-8") as f:
            return f.read()

    def write(self, rel, text):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def grow(self, name, n):
        """Дописать сверху хроники n новых записей (файла нет — взять образец)."""
        path = os.path.join(self.dd, name)
        text = self.read("memory", "projects", name) if os.path.exists(path) else SAMPLE
        new = "".join("- 01.10 %02d:%02d · [агент] тест · запись %d · проверка\n" % (10 + i // 60, i % 60, i)
                      for i in range(n))
        idx = text.index("\n", text.index("## Хроника")) + 1
        text = text[:idx] + new + text[idx:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return text


class Samples(Base):
    def test_short_untouched(self):
        self.write("memory/projects/Site.md", SAMPLE)
        self.write("memory/NOW.md", "# Сейчас\n")
        before = snapshot(self.home)
        lines = mem_guard.check(self.home, NOW)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(lines, [])

    def test_long_chronicle_moves_oldest_verbatim(self):
        self.write("memory/NOW.md", "# Сейчас\n")
        self.write("memory/people.md", "# Люди\n")
        old = self.grow("Site.md", 15)
        old_chron = chronicle(old)
        self.assertGreater(len(old_chron), 25)
        lines = mem_guard.check(self.home, NOW)
        new = self.read("memory", "projects", "Site.md")
        new_chron = chronicle(new)
        self.assertLessEqual(len(new_chron), 20)
        self.assertEqual(old_chron[:len(new_chron)], new_chron)
        self.assertEqual(new[:new.index("## Хроника")], old[:old.index("## Хроника")])  # до «Хроники» — байт в байт
        self.assertTrue(new.endswith("## Решения\n- без трекеров\n"))
        arch = self.read("archive", "Site-2026-10.md")
        body = [ln + "\n" for ln in arch.splitlines() if ln.startswith(("- ", "  "))]
        self.assertEqual(body, old_chron[len(new_chron):])
        self.assertIn("## 01.10.2026 12:00 · из «Хроники» memory/projects/Site.md", arch)
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertEqual(self.read("memory", "NOW.md"), "# Сейчас\n")
        self.assertEqual(self.read("memory", "people.md"), "# Люди\n")

    def test_entry_with_continuation_not_split(self):
        old = self.grow("Site.md", 19)  # запись с продолжением — ровно на границе 20
        mem_guard.check(self.home, NOW)
        new = self.read("memory", "projects", "Site.md")
        arch = self.read("archive", "Site-2026-10.md")
        self.assertEqual(len(chronicle(new)), 19)
        self.assertNotIn("⚠ исправлено", new[new.index("## Хроника"):])
        i = arch.index("- 01.10 09:00 · [агент] Сайт · шаг с продолжением")
        self.assertTrue(arch[i:].split("\n", 2)[1].startswith("  ⚠ исправлено"))
        self.assertEqual(chronicle(old), chronicle(new) + [ln + "\n" for ln in arch.splitlines()
                                                           if ln.startswith(("- ", "  "))])

    def test_second_run_appends_to_archive(self):
        self.grow("Site.md", 20)
        mem_guard.check(self.home, NOW)
        first = self.read("archive", "Site-2026-10.md")
        self.grow("Site.md", 10)
        mem_guard.check(self.home, datetime(2026, 10, 1, 13, 30))
        second = self.read("archive", "Site-2026-10.md")
        self.assertTrue(second.startswith(first))
        self.assertEqual(second.count("# Site · архив хроники 2026-10"), 1)
        self.assertIn("## 01.10.2026 13:30 · из «Хроники» memory/projects/Site.md", second[len(first):])

    def test_speed_under_150ms(self):
        for n in range(15):
            self.write("memory/projects/P%d.md" % n, SAMPLE)
        for d in range(1, 29):
            self.write("journal/2026-09-%02d.md" % d, "- строка\n" * 200)
        mem_guard.check(self.home, NOW)  # первый проход переносит старое
        best = min(self._timed() for _ in range(3))
        self.assertLess(best, 0.150, "check() шёл %.0f мс" % (best * 1000))

    def _timed(self):
        t = time.perf_counter()
        mem_guard.check(self.home, NOW)
        return time.perf_counter() - t


class Chronicle(Base):
    def dossier(self, n, after="## Потом\n- не трогать\n"):
        body = "".join("- %02d · запись\n" % i for i in range(n))
        text = "# X · тест\nОбновлено 01.10\n\n## Сейчас\n- a\n\n## Хроника (новое сверху)\n" + body + "\n" + after
        self.write("memory/projects/X.md", text)
        return text

    def test_25_lines_stay(self):
        text = self.dossier(25)
        mem_guard.check(self.home, NOW)
        self.assertEqual(self.read("memory", "projects", "X.md"), text)
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "state")))

    def test_26_lines_trim_to_20_and_keep_next_section(self):
        self.dossier(26)
        mem_guard.check(self.home, NOW)
        new = self.read("memory", "projects", "X.md")
        self.assertEqual(chronicle(new), ["- %02d · запись\n" % i for i in range(20)])
        self.assertTrue(new.endswith("- 19 · запись\n\n## Потом\n- не трогать\n"))
        arch = self.read("archive", "X-2026-10.md")
        self.assertTrue(arch.endswith("".join("- %02d · запись\n" % i for i in range(20, 26))))

    def test_newest_entry_stays_even_if_huge(self):
        big = "- новое\n" + "".join("  продолжение %d\n" % i for i in range(22))
        self.write("memory/projects/X.md", "## Хроника\n" + big + "".join("- %d\n" % i for i in range(5)))
        mem_guard.check(self.home, NOW)
        self.assertEqual(self.read("memory", "projects", "X.md"), "## Хроника\n" + big)

    def test_protected_files_untouched(self):
        long = "## Хроника\n" + "".join("- %d\n" % i for i in range(40))
        for rel in ("memory/NOW.md", "memory/people.md", "memory/projects/_draft.md"):
            self.write(rel, long)
        before = snapshot(self.home)
        mem_guard.check(self.home, NOW)
        self.assertEqual({k: v for k, v in snapshot(self.home).items() if k in before}, before)
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))

    def test_changed_since_read_is_left_for_next_time(self):
        self.dossier(30)
        path = os.path.join(self.dd, "X.md")
        real_read = mem_guard._read_bytes
        calls = []

        def racing_read(p):
            calls.append(p)
            if len(calls) == 2:  # Рик правит файл проекта, пока mem_guard готовит замену
                with open(p, "a", encoding="utf-8") as f:
                    f.write("- правка Рика\n")
            return real_read(p)

        with mock.patch.object(mem_guard, "_read_bytes", racing_read):
            warns, actions = mem_guard._run(self.home, NOW, False)
        self.assertEqual(calls, [path, path])
        self.assertTrue(any("менялся во время правки" in a for a in actions))
        new = self.read("memory", "projects", "X.md")
        self.assertTrue(new.endswith("- правка Рика\n"))
        self.assertEqual(len(chronicle(new)), 30)
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive", "X-2026-10.md")))
        self.assertEqual([n for n in os.listdir(self.dd) if n.endswith(".tmp")], [])

    def test_rollback_keeps_existing_archive(self):
        self.dossier(30)
        arch = self.write("archive/X-2026-10.md", "# X · архив хроники 2026-10\nстарое\n")
        with open(arch, "rb") as f:
            before = f.read()
        real_read = mem_guard._read_bytes
        calls = []

        def racing_read(p):
            calls.append(p)
            if len(calls) == 2:
                with open(p, "a", encoding="utf-8") as f:
                    f.write("x")
            return real_read(p)

        with mock.patch.object(mem_guard, "_read_bytes", racing_read):
            mem_guard.check(self.home, NOW)
        self.assertEqual(len(calls), 2)
        with open(arch, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_busy_lock_skips_moves(self):
        text = self.dossier(30)
        os.makedirs(os.path.join(self.home, "state"))
        fd = os.open(os.path.join(self.home, "state", ".mem_guard.lock"), os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        warns, actions = mem_guard._run(self.home, NOW, False)
        self.assertEqual(self.read("memory", "projects", "X.md"), text)
        self.assertTrue(any("уже работает" in a for a in actions))

    def test_dry_writes_nothing(self):
        self.dossier(40)
        self.write("journal/2026-08-01.md", "старый\n")
        self.write("memory/people.md", "x" * 9000)
        before = snapshot(self.home)
        warns, actions = mem_guard._run(self.home, NOW, True)
        self.assertEqual(snapshot(self.home), before)
        self.assertTrue(any(a.startswith("(dry) memory/projects/X.md") for a in actions))
        self.assertTrue(any(a.startswith("(dry) journal/2026-08-01.md") for a in actions))
        self.assertTrue(any(w.startswith("⚠ memory/people.md") for w in warns))


class Journals(Base):
    def test_older_than_30_days_move(self):
        for name in ("2026-08-31.md", "2026-09-01.md", "2026-10-01.md", "notes.md", "2026-02-30.md"):
            self.write("journal/" + name, name)
        mem_guard.check(self.home, NOW)
        jd = os.path.join(self.home, "journal")
        self.assertEqual(sorted(os.listdir(jd)), ["2026-02-30.md", "2026-09-01.md", "2026-10-01.md", "notes.md"])
        self.assertEqual(self.read("archive", "journal", "2026-08", "2026-08-31.md"), "2026-08-31.md")

    def test_existing_archive_not_overwritten(self):
        self.write("journal/2026-07-10.md", "новое")
        self.write("archive/journal/2026-07/2026-07-10.md", "старое")
        lines = mem_guard.check(self.home, NOW)
        self.assertEqual(self.read("journal", "2026-07-10.md"), "новое")
        self.assertEqual(self.read("archive", "journal", "2026-07", "2026-07-10.md"), "старое")
        self.assertTrue(any(line.startswith("⚠ journal/2026-07-10.md:") for line in lines))


class Warnings(Base):
    def test_limits_and_cap(self):
        self.write("memory/NOW.md", "н" * 2100)          # 4200 байт
        self.write("memory/people.md", "л" * 2100)
        self.write("memory/projects/A.md", "a" * 8100)
        self.write("memory/projects/B.md", "b" * 9500)
        self.write("memory/projects/C.md", "c" * 9000)
        t = time.time()
        os.utime(os.path.join(self.home, "memory", "NOW.md"), (t - 4 * 3600, t - 4 * 3600))
        os.utime(os.path.join(self.dd, "A.md"), (t, t))
        for name in ("B.md", "C.md"):
            os.utime(os.path.join(self.dd, name), (t - 5 * 3600, t - 5 * 3600))
        lines = mem_guard.check(self.home, NOW)
        self.assertEqual(len(lines), 5)
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertTrue(lines[0].startswith("⚠ memory/NOW.md: 4,2 КБ при потолке 4"))
        self.assertTrue(lines[1].startswith("⚠ memory/NOW.md: старше memory/projects/A.md на 4 ч 00 мин"))
        self.assertTrue(lines[2].startswith("⚠ memory/people.md: 4,2 КБ"))
        self.assertTrue(lines[3].startswith("⚠ memory/projects/B.md: 9,5 КБ"))
        self.assertTrue(lines[4].startswith("⚠ memory/projects/C.md: 9,0 КБ"))

    def test_at_limit_quiet_and_just_over_precise(self):
        self.write("memory/NOW.md", "n" * 4000)
        self.write("memory/projects/C.md", "c" * 8000)
        self.write("memory/projects/D.md", "d" * 8029)
        t = time.time()
        os.utime(os.path.join(self.home, "memory", "NOW.md"), (t, t))
        for name in ("C.md", "D.md"):
            os.utime(os.path.join(self.dd, name), (t, t))
        self.assertEqual(mem_guard.check(self.home, NOW),
                         ["⚠ memory/projects/D.md: 8,03 КБ при потолке 8 — сжать «Сейчас» и «Решения», старое — в "
                          "archive/"])

    def test_fresh_now_is_quiet(self):
        self.write("memory/NOW.md", "n")
        self.write("memory/projects/A.md", "a")
        t = time.time()
        os.utime(os.path.join(self.home, "memory", "NOW.md"), (t - 3600, t - 3600))
        os.utime(os.path.join(self.dd, "A.md"), (t, t))
        self.assertEqual(mem_guard.check(self.home, NOW), [])


class NeverThrows(Base):
    def test_bad_inputs(self):
        self.assertEqual(mem_guard.check(os.path.join(self.home, "нет"), NOW), [])
        self.write("file", "x")
        self.assertEqual(mem_guard.check(os.path.join(self.home, "file"), NOW), [])
        with open(os.path.join(self.dd, "Bad.md"), "wb") as f:
            f.write(b"## \xd0\xa5\xff\xfe\n" + b"- x\n" * 40)
        self.assertIsInstance(mem_guard.check(self.home, NOW), list)
        self.assertIsInstance(mem_guard.check(self.home, "не дата"), list)
        self.assertIsInstance(mem_guard.check(123, NOW), list)

    def test_env_home(self):
        self.write("memory/NOW.md", "b" * 8500)
        with mock.patch.dict(os.environ, {"RICK_HOME": self.home}):
            lines = mem_guard.check(now=NOW)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("⚠ memory/NOW.md: 8,5 КБ"))


class Cli(Base):
    SCRIPT = os.path.join(TOOLS, "mem_guard.py")

    def run_cli(self, python, *args):
        env = dict(os.environ, RICK_HOME=self.home)
        return subprocess.run([python, self.SCRIPT] + list(args), capture_output=True, text=True, env=env,
                              timeout=30)

    def setUp(self):
        super().setUp()
        self.write("memory/NOW.md", "b" * 9000)
        self.write("memory/projects/X.md", "## Хроника\n" + "".join("- %d\n" % i for i in range(30)))
        self.before = snapshot(self.home)

    def test_dry(self):
        r = self.run_cli(sys.executable, "--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.splitlines()
        self.assertTrue(out[0].startswith("⚠ memory/NOW.md: 9,0 КБ"))
        self.assertIn("· (dry) memory/projects/X.md: «Хроника» 30 строк — 10 старых перенёс бы в archive/X-", r.stdout)
        self.assertEqual(snapshot(self.home), self.before)

    def test_no_memory_is_not_clean(self):
        env = dict(os.environ, RICK_HOME=os.path.join(self.home, "нет"))
        r = subprocess.run([sys.executable, self.SCRIPT], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ничего не проверено", r.stderr)

    def test_bad_arg(self):
        r = self.run_cli(sys.executable, "--wet")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(snapshot(self.home), self.before)

    def test_python39(self):
        py39 = "/usr/bin/python3"
        if not os.path.exists(py39):
            self.skipTest("нет /usr/bin/python3")
        r = self.run_cli(py39, "--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("⚠ memory/NOW.md", r.stdout)
        self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
