"""Тесты tools/digest.py на выдуманной папке проектов и подменном tmux (RICK_TMUX): настоящий tmux не нужен."""
import contextlib
import datetime
import html.parser
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import digest  # noqa: E402

FAKE_TMUX = '''import os, sys
if sys.argv[1] == "list-sessions":
    sys.stdout.write(os.environ.get("FAKE_SESSIONS", "").replace("|", "\\t").replace(";", "\\n"))
'''

STATUS_RUN = """# 🧪 Site · сайт-визитка
обновлено 10:15

3 воркфлоу
1 готово
2,5 ч потрачено

1️⃣ Workflow ✅ готово
- о чём: макет главной
- шаги 2 из 2

2️⃣ Workflow 💭 идёт
- о чём: форма заявки и проверка полей
- шаги 1 из 3
- 13:05 → закончит в 13:50 · идёт 25 мин

3️⃣ Workflow ⬜ запланирован
- о чём: выкладка
- шаги 0 из 2

Сейчас: верстаю форму.
Ждёт хозяина: выбрать шрифт — рекомендую: второй, он читается на телефоне.
"""
STATUS_WAIT = "# Blog\n\n1️⃣ Workflow ✅ готово\n- о чём: пост\n\nЖдёт хозяина: <b>можно</b> публиковать?\n"
STATUS_QUIET = "# Notes\n\n1️⃣ Workflow ✅ готово\n\nСейчас: ничего.\nЖдёт хозяина: нет.\n"
STATUS_OLD = "# Shop\n\n1️⃣ Workflow 💭 идёт\n- о чём: корзина\n\nЖдёт хозяина: ключ платёжки.\n"
OLD_FORMAT = "# Old\n\n✅ Workflow готово\n- о чём: старое\n⏳ Workflow идёт\n- шаги 2 из 5\n\nЖдёт пользователя: да или нет\n"

VOID = {"meta", "br", "input", "img", "hr", "link", "wbr", "source"}


class Balance(html.parser.HTMLParser):
    """Статическая проверка разметки: каждый открытый тег закрыт в своём порядке."""

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append("лишний </%s> при %s" % (tag, self.stack[-3:]))
        else:
            self.stack.pop()


def write(path, text, age=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if age:
        old = time.time() - age
        os.utime(path, (old, old))


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "rick")
        self.root = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.home, "templates"))
        shutil.copy(os.path.join(ROOT, "templates", "digest.html"), os.path.join(self.home, "templates"))
        write(os.path.join(self.root, "Site", "docs", "status.md"), STATUS_RUN)
        write(os.path.join(self.root, "Blog", "status.md"), STATUS_WAIT)
        write(os.path.join(self.root, "Notes", "docs", "status.md"), STATUS_QUIET)
        write(os.path.join(self.root, "Shop", "docs", "status.md"), STATUS_OLD, age=2 * 86400)
        os.makedirs(os.path.join(self.root, "Bare"))                       # ни сводки, ни чата — не проект
        os.makedirs(os.path.join(self.root, "Live"))                       # без сводки, но с живым чатом
        write(os.path.join(self.root, "Site-wt", ".git"), "gitdir: ../Site/.git/worktrees/x")
        write(os.path.join(self.root, "Site-wt", "docs", "status.md"), STATUS_RUN)   # рабочее дерево — мимо
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"projects_root": self.root}, fh)
        fake = os.path.join(self.tmp, "tmux")
        write(fake, "#!%s\n%s" % (sys.executable, FAKE_TMUX))
        os.chmod(fake, 0o755)
        env = {"RICK_HOME": self.home, "RICK_TMUX": fake,
               "FAKE_SESSIONS": "rick-Live|%s;rick-Site|%s;other|/tmp" % (
                   os.path.join(self.root, "Live"), os.path.join(self.root, "Site"))}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def items(self):
        return {i["project"]: i for i in digest.collect(self.root, digest.sessions(), time.time())}

    def test_parse_contract_format(self):
        p = digest.parse(STATUS_RUN)
        self.assertEqual(p["title"], "🧪 Site · сайт-визитка")
        self.assertEqual([w["state"] for w in p["workflows"]], ["готово", "идёт", "запланирован"])
        self.assertEqual(p["workflows"][1], {"state": "идёт", "about": "форма заявки и проверка полей", "steps": "1/3"})
        self.assertEqual(p["now"], "верстаю форму.")
        self.assertTrue(p["wait"].startswith("выбрать шрифт"))
        self.assertIn("3 воркфлоу", p["summary"])

    def test_parse_key_lines_not_heads(self):
        # Строки «Ключ: …» с упоминанием Workflow — не заголовки блоков, в каком бы месте ни стояли.
        text = ("# P\nИтог: Workflow 1 и Workflow 2 сделаны\n\n1️⃣ Workflow ✅ готово\n"
                "Сейчас: Workflow 2 на проверке.\nЖдёт хозяина: Workflow 3 запускать?\n"
                "Workflow: см. план\nЗаметка — Workflow без двоеточия в начале\n")
        p = digest.parse(text)
        self.assertEqual([w["state"] for w in p["workflows"]], ["готово"])
        self.assertEqual(p["now"], "Workflow 2 на проверке.")
        self.assertEqual(p["wait"], "Workflow 3 запускать?")
        self.assertIn("Итог: Workflow 1 и Workflow 2 сделаны", p["summary"])

    def test_parse_states(self):
        text = ("1️⃣ Workflow ⬜\n2️⃣ Workflow запланирован\n3️⃣ Workflow 🛑 упал\n"
                "4️⃣ Workflow 💭\n5️⃣ Workflow ✅\n6️⃣ Workflow\nWorkflow 7 идёт\n")
        self.assertEqual([w["state"] for w in digest.parse(text)["workflows"]],
                         ["запланирован", "запланирован", "упал", "идёт", "готово", "ждёт", "идёт"])

    def test_parse_old_format(self):
        p = digest.parse(OLD_FORMAT)
        self.assertEqual([w["state"] for w in p["workflows"]], ["готово", "идёт"])
        self.assertEqual(p["workflows"][1]["steps"], "2/5")
        self.assertEqual(p["wait"], "да или нет")

    def test_groups(self):
        got = self.items()
        self.assertEqual(sorted(got), ["Blog", "Live", "Notes", "Shop", "Site"])
        self.assertEqual(got["Site"]["group"], "идут")
        self.assertEqual(got["Site"]["running"], [{"n": 2, "about": "форма заявки и проверка полей", "steps": "1/3"}])
        self.assertEqual((got["Site"]["done"], got["Site"]["workflows"]), (1, 3))
        self.assertEqual(got["Site"]["terminal"], "rick-Site")
        self.assertEqual(got["Blog"]["group"], "ждут")
        self.assertEqual(got["Notes"]["group"], "тихо")
        self.assertEqual(got["Notes"]["wait"], "")
        self.assertTrue(got["Shop"]["stale"])
        self.assertEqual(got["Shop"]["group"], "ждут")      # «идёт» в сводке старше суток — не идёт
        self.assertEqual((got["Live"]["group"], got["Live"]["terminal"]), ("тихо", "rick-Live"))

    def test_no_tmux_no_root(self):
        with mock.patch.dict(os.environ, {"RICK_TMUX": os.path.join(self.tmp, "нет-такого")}):
            self.assertEqual(digest.sessions(), {})
        self.assertEqual(digest.collect(os.path.join(self.tmp, "нет"), {}, time.time()), [])
        self.assertIn("Сводок проектов нет", digest.text([]))

    def test_text(self):
        out = digest.text(list(self.items().values()))
        self.assertIn("⏳ Идут — 1", out)
        self.assertIn("2-й идёт (шаги 1 из 3): форма заявки", out)
        self.assertIn("Ждёт: выбрать шрифт", out)                 # вопрос идущего проекта не теряется
        self.assertIn("✋ Ждут хозяина — 3", out)                # идущий проект с вопросом — тоже ждёт
        self.assertIn("ещё в «Идут»: 🧪 Site · сайт-визитка", out)
        self.assertIn("сводка старше суток", out)
        self.assertIn("· Тихо: Live, Notes", out)

    def test_html(self):
        when = datetime.datetime(2026, 1, 2, 3, 4)
        path = digest.write_html(list(self.items().values()), self.home, when)
        self.assertEqual(path, os.path.join(self.home, "state", "digest-20260102-0304.html"))
        with open(path, encoding="utf-8") as fh:
            page = fh.read()
        checker = Balance()
        checker.feed(page)
        checker.close()
        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])
        self.assertNotIn("{{", page)
        self.assertIn('name="viewport"', page)
        self.assertIn("prefers-color-scheme:light", page)
        self.assertNotIn("http", page)                            # без сети
        self.assertNotIn("<b>можно</b>", page)                    # текст сводки экранирован
        self.assertIn("&lt;b&gt;можно&lt;/b&gt;", page)
        # «Реши»: свежие вопросы 1–2, «Подвисшие» продолжают нумерацию
        self.assertEqual(page.count('id="q1"'), 1)               # контент не продублирован (комментарий шаблона)
        self.assertEqual(page.count("<h1>Что у нас</h1>"), 1)
        self.assertLess(page.index('id="q1"'), page.index('id="q2"'))
        self.assertLess(page.index('id="g-hung"'), page.index('id="q3"'))
        self.assertIn('data-project="Shop"', page)
        self.assertIn("рекомендую:</i> второй, он читается на телефоне.", page)
        self.assertIn("Скопировать ответы", page)
        self.assertIn("<b>Тихо:</b> Live, Notes", page)
        self.assertIn("ждут тебя 3 ·", page)                      # шапка считает все вопросы, и идущего тоже
        self.assertEqual(page.count('class="pt" id="q'), 3)

    def test_header_counts_running_question(self):
        item = {"project": "X", "title": "X", "group": "идут", "workflows": 1, "done": 0,
                "running": [{"n": 1, "about": "a", "steps": ""}], "summary": [], "now": "", "wait": "шрифт?",
                "stale": False, "terminal": "", "updated": ""}
        page = digest.render([item], datetime.datetime(2026, 1, 2), "{{HEADER}}|{{BODY}}")
        self.assertIn("идут 1 · ждут тебя 1 · тихо 0", page)
        self.assertIn('id="q1"', page)
        out = digest.text([item])
        self.assertIn("✋ Ждут хозяина — 1", out)
        self.assertIn("ещё в «Идут»: X", out)

    def test_template_marks_only_in_markup(self):
        # Метки — только по одной и вне комментариев, иначе подстановка копирует контент.
        with open(os.path.join(ROOT, "templates", "digest.html"), encoding="utf-8") as fh:
            tpl = fh.read()
        for mark in ("{{TITLE}}", "{{HEADER}}", "{{BODY}}"):
            self.assertEqual(tpl.count(mark), 1, mark)
        self.assertEqual(digest.PLACEHOLDER_RE.findall(tpl), ["TITLE", "HEADER", "BODY"])

    def test_render_single_pass(self):
        item = {"project": "X", "title": "{{BODY}}", "group": "ждут", "workflows": 0, "done": 0, "running": [],
                "summary": [], "now": "", "wait": "{{HEADER}} и {{TITLE}}", "stale": False, "terminal": "",
                "updated": ""}
        page = digest.render([item], datetime.datetime(2026, 1, 2), "<t>{{TITLE}}</t>{{HEADER}}|{{BODY}}")
        self.assertIn("{{HEADER}} и {{TITLE}}", page)            # текст сводки не подставляется второй раз
        self.assertEqual(page.count("<h1>Что у нас</h1>"), 1)

    def test_html_empty(self):
        with open(os.path.join(self.home, "templates", "digest.html"), encoding="utf-8") as fh:
            page = digest.render([], datetime.datetime(2026, 1, 2), fh.read())
        self.assertIn("Сводок нет", page)
        self.assertNotIn('id="q1"', page)

    def test_main_html_open(self):
        out = io.StringIO()
        with mock.patch.object(digest.subprocess, "run") as run, contextlib.redirect_stdout(out):
            self.assertEqual(digest.main(["--open"]), 0)
        path = out.getvalue().strip()
        self.assertTrue(path.startswith(os.path.join(self.home, "state", "digest-")) and os.path.isfile(path))
        self.assertEqual(run.call_args[0][0][-1], path)

    def test_main_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(digest.main(["--json"]), 0)
        self.assertEqual(len(json.loads(out.getvalue())), 5)
        self.assertFalse(os.path.exists(os.path.join(self.home, "state")))   # без --html файлов не пишет


if __name__ == "__main__":
    unittest.main()
