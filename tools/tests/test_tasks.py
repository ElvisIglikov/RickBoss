"""Тесты tools/tasks.py: номера #N, последняя запись побеждает, ничего не удаляется, файл — только после add."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)
import tasks  # noqa: E402

PY = sys.executable


class TasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"RICK_HOME": self.home})
        self.env.start()
        self.file = os.path.join(self.home, "memory", "tasks.jsonl")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *args, stdin=None):
        r = subprocess.run([PY, os.path.join(TOOLS, "tasks.py"), *args], input=stdin, capture_output=True, text=True,
                           env=dict(os.environ, RICK_HOME=self.home), timeout=20)
        return r.returncode, r.stdout + r.stderr

    def test_empty_memory_creates_nothing(self):
        self.assertEqual(self.cli("list"), (0, "задач нет\n"))
        self.assertEqual(tasks.brief(), "")
        for args in (("show", "1"), ("start", "1"), ("done", "1"), ("cancel", "1", "--reason", "x")):
            code, out = self.cli(*args)
            self.assertEqual(code, 2, args)
            self.assertIn("задачи #1 нет", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "memory")))

    def test_add_list_show_and_lifecycle(self):
        code, out = self.cli("add", "Починить сайт", "--project", "Сайт", "--original", "почини сайт, звони +7 900 "
                             "000-00-06", "--summary", "кнопка не жмётся", "--start")  # public-ok выдуманный
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("#1 💭 Починить сайт · Сайт · "), out)
        self.assertEqual(self.cli("add", "Отчёт")[1].split()[:2], ["#2", "⬜"])
        self.assertNotIn("000-00-06", self.cli("show", "1")[1])   # телефон из дословного — скрыт
        self.assertIn("summary: кнопка не жмётся", self.cli("show", "#1")[1])
        self.assertEqual(self.cli("done", "1", "--result", "кнопка жмётся")[0], 0)
        code, out = self.cli("cancel", "2", "--reason", "дубль #1")
        self.assertIn("#2 ✖ Отчёт", out)
        self.assertEqual(self.cli("start", "1")[0], 2)   # закрытую не переоткрыть: новое дело — новая задача
        self.assertIn("result: кнопка жмётся", self.cli("show", "1")[1])
        self.assertEqual(self.cli("list", "--open"), (0, "задач нет\n"))
        self.assertEqual(len(self.cli("list")[1].splitlines()), 2)
        with open(self.file, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh]
        self.assertEqual([r["id"] for r in rows], [1, 2, 1, 2])   # не удаляем — дописываем
        self.assertEqual(set(rows[0]), {"id", "title", "project", "original", "summary", "status", "result",
                                        "created", "updated"})
        self.assertEqual(self.cli("add", "  ")[0], 2)

    def test_add_json_batch_filter_and_brief(self):
        batch = [{"title": "А", "project": "Сайт", "original": "сделай а"}, {"title": "Б", "project": "Бот"},
                 {"title": "В", "project": "сайт", "start": True}]
        code, out = self.cli("add-json", "-", stdin=json.dumps(batch, ensure_ascii=False))
        self.assertEqual(code, 0, out)
        self.assertEqual([x.split()[0] for x in out.splitlines()], ["#1", "#2", "#3"])
        self.assertEqual(sorted(x.split()[0] for x in self.cli("list", "--project", "Сайт")[1].splitlines()),
                         ["#1", "#3"])
        self.assertEqual(len(self.cli("list", "--limit", "1")[1].splitlines()), 1)
        brief = tasks.brief(limit=2).splitlines()
        self.assertTrue(brief[0].startswith("задач открыто 3"), brief)
        self.assertEqual(len(brief), 3)
        for bad in ("не json", "[]", '[{"project": "x"}]', "{}"):
            self.assertEqual(self.cli("add-json", "-", stdin=bad)[0], 2, bad)
        self.assertEqual(len(tasks.load()), 3)   # плохая пачка не дописала ничего

    def test_parallel_adds_get_unique_numbers(self):
        threads = [threading.Thread(target=self.cli, args=("add", "задача %d" % i)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(tasks.load()), list(range(1, 9)))

    def test_broken_line_skipped(self):
        tasks.add([{"title": "раз"}])
        with open(self.file, "a", encoding="utf-8") as fh:
            fh.write('{"id": 2, "title": "обор')
        self.assertEqual(list(tasks.load()), [1])
        tasks.add([{"title": "два"}])   # новая запись — с новой строки, оборванная её не портит
        self.assertEqual({k: v["title"] for k, v in tasks.load().items()}, {1: "раз", 2: "два"})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(tasks.main(["list"]), 0)


if __name__ == "__main__":
    unittest.main()
