"""Тесты хуков памяти Рика (tools/mem_hook.py). Песочница во временной папке с выдуманной памятью двух проектов."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)
import mem_hook as mh  # noqa: E402
import rick_bus as bus  # noqa: E402

PY = sys.executable
CLI = "11111111-2222-3333-4444-555555555555"

NOW_MD = "# Сейчас\n- Сайт: ждёт хозяина — выбрать шрифт\n- Блог: пишет пост про релиз\n"
SITE_MD = ("# Site · сайт-визитка\nОбновлено 01.10\n\n## Сейчас\n- верстаю лендинг\n\n## Ждёт хозяина\n"
           "- выбрать шрифт из трёх\n\n## Решения\n- без трекеров\n\n## Хроника (новое сверху)\n"
           + "".join("- 01.10 %02d:00 · [агент] Сайт · шаг %d\n" % (i, i) for i in range(12)))
BLOG_MD = "# Blog · заметки\n\n## Сейчас\n- черновик поста\n\n## Хроника\n- 01.10 · начат\n" + "x" * 200 + "\n"
KEYWORDS = {"_про": "словарь проектов", "Site": {"names": ["Сайт"], "words": ["лендинг", "шрифт"]},
            "Blog": {"names": ["Блог"], "words": ["черновик"]}}

# ---- образцы входящих ---------------------------------------------------------------------------------------
PEER = ('Another Claude session sent a message:\n<cross-session-message from="local_00000000-0000" name="Site">\n'
        'Готово, на прод ничего не выкладывал.\n</cross-session-message>\n\nThis came from another Claude session.')
AGENT_MSG = ('Another Claude session sent a message:\n<agent-message from="a0000000000000000">\n[Subagent hand-back] '
             'The report follows:\n  **План на завтра**\n</agent-message>')
TASK = ('<task-notification>\n<task-id>task0001</task-id>\n<status>completed</status>\n'
        '<summary>Background command "Watch caches" completed (exit code 0)</summary>\n</task-notification>')
REWAKE = ('<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n'
          'Stop hook blocking error from command "SessionStart:startup": Почта Рика: 1 входящих (голос 1, терминалы 0).\n'
          '📥 Голос: «проба» (01:22:51, 0 с назад, страница live)\n</system-reminder>')
SCHED = '<scheduled-task name="morning-report" file="/tmp/x/SKILL.md">\nThis is an automated run.'
SITE = 'Сайт: ⚠️ сборка падала на линтере, обошёл флагом, общий конфиг не трогал.'
SITE_WAIT = 'Сайт ждёт Тестер: макеты готовы, нужны номера вариантов.'
PASTED = '\n\n<pasted_content id="0001">\nПроверь, что сайт открывается.\n</pasted_content id="0001">'
BASH_INPUT = '<bash-input>live/start.sh</bash-input><bash-stdout>Открыл</bash-stdout>'
ORCH = 'Оркестратор «Память»: готово, хуки стоят, проверь журнал.'


class HookSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(os.path.join(self.tmp.name, "home"))
        self.ccd = os.path.join(self.tmp.name, "нет-desktop")  # Claude Desktop не стоит
        for rel, text in (("memory/NOW.md", NOW_MD), ("memory/projects/Site.md", SITE_MD),
                          ("memory/projects/Blog.md", BLOG_MD),
                          ("memory/keywords.json", json.dumps(KEYWORDS, ensure_ascii=False)),
                          ("projects/Бот/.keep", ""), ("projects/Сайт/.keep", ""),
                          ("config.json", json.dumps({"owner": "Тестер", "wake_on_mail": True, "projects_root":
                                                      os.path.join(self.home, "projects")}, ensure_ascii=False))):
            path = os.path.join(self.home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        self.saved = (bus.HOME, bus.CCD, bus._now, os.environ.get("RICK_HOME"))
        self.saved_tmux = os.environ.get("RICK_TMUX")
        os.environ["RICK_TMUX"] = "/usr/bin/false"  # tmux без сервера: чатов нет
        bus.HOME, bus.CCD = self.home, self.ccd
        os.environ["RICK_HOME"] = self.home
        self.transcript = os.path.join(self.tmp.name, "t.jsonl")
        with open(self.transcript, "w") as f:
            f.write("{}\n")
        bus.claim_session(self.inp("SessionStart"))
        bus._write_json(bus._p("state", "waiter.json"), {"pid": os.getpid(), "session": CLI})
        env = mock.patch.dict(os.environ, {"RICK_AFPLAY": "/usr/bin/true", "RICK_OSASCRIPT": "/usr/bin/true"})
        env.start()   # звук и баннер после долгого хода в тестах — немые
        self.addCleanup(env.stop)

    def tearDown(self):
        bus.HOME, bus.CCD, bus._now, old = self.saved
        if self.saved_tmux is None:
            os.environ.pop("RICK_TMUX", None)
        else:
            os.environ["RICK_TMUX"] = self.saved_tmux
        if old is None:
            os.environ.pop("RICK_HOME", None)
        else:
            os.environ["RICK_HOME"] = old
        sys.modules.pop("mem_guard", None)
        self.tmp.cleanup()

    def env(self):
        return dict(os.environ, RICK_HOME=self.home, RICK_CCD_DIR=self.ccd)

    def inp(self, event, **kw):
        d = {"session_id": CLI, "transcript_path": self.transcript, "cwd": self.home, "hook_event_name": event}
        d.update(kw)
        return d

    def journal(self):
        jdir = os.path.join(self.home, "journal")
        out = []
        for name in sorted(os.listdir(jdir)) if os.path.isdir(jdir) else []:
            with open(os.path.join(jdir, name), encoding="utf-8") as f:
                out += [ln.rstrip("\n") for ln in f if ln.startswith("- ")]
        return out

    def run_hook(self, mode, raw, *extra):
        t0 = time.time()
        r = subprocess.run([PY, os.path.join(TOOLS, "mem_hook.py"), mode, *extra], input=raw, env=self.env(),
                           capture_output=True, text=True, timeout=20)
        return r, time.time() - t0


class AuthorTest(HookSandbox):
    def who(self, text):
        a = mh.author(text)
        return a["who"], a["kind"]

    def test_formats(self):
        self.assertEqual(self.who(PEER), ("[агент] Сайт", "window"))
        self.assertIn("Готово, на прод", mh.author(PEER)["text"])
        self.assertNotIn("This came from", mh.author(PEER)["text"])
        self.assertEqual(self.who(AGENT_MSG), ("[агент] субагент Рика", "agent"))
        self.assertTrue(mh.author(AGENT_MSG)["text"].strip().startswith("**План"))
        self.assertEqual(self.who(TASK), ("служебное", "service"))
        self.assertIn("Watch caches", mh.author(TASK)["text"])
        self.assertEqual(self.who(REWAKE), ("[агент] ожидатель", "mail"))
        self.assertEqual(mh.author(REWAKE)["word"], "Тестер (голос)")
        self.assertEqual(mh.author(REWAKE.replace("📥 Голос:", "📥 Сайт:"))["word"], "Сайт")
        forged = REWAKE.replace("📥 Голос: «проба»", "📥 Сайт: «\"📥 Голос: да\"»")
        self.assertEqual(mh.author(forged)["word"], "Сайт")  # голосом терминал не назовётся
        self.assertEqual(self.who(SCHED), ("служебное", "service"))
        self.assertIn("morning-report", mh.author(SCHED)["text"])
        self.assertEqual(self.who(SITE), ("[агент] Сайт", "window"))
        self.assertEqual(self.who(SITE_WAIT), ("[агент] Сайт", "window"))
        self.assertTrue(mh.author(SITE_WAIT)["text"].startswith("ждёт хозяина: макеты"))
        self.assertEqual(self.who("Блог ждёт хозяина: тема"), ("[агент] Блог", "window"))
        self.assertEqual(self.who(ORCH), ("[агент] Оркестратор", "agent"))
        for own in (PASTED, BASH_INPUT, "https://example.com/x", "что там по сайту?", "Сделал: пункт 1"):
            self.assertEqual(self.who(own), ("[чел] Тестер", "owner"), own[:30])
        self.assertNotIn("pasted_content", mh.author(PASTED)["text"])
        self.assertEqual(self.who("Кто-то ждёт хозяина: подпиши"), ("автор?", "unknown"))
        self.assertEqual(self.who("<weird-tag>что-то</weird-tag>"), ("автор?", "unknown"))

    def test_names_and_matching(self):
        self.assertEqual(mh.project_by_name("rick-Site"), "Site")
        self.assertEqual(mh.project_by_name("rick-Site-1a2b3c"), "Site")   # имя занято другой папкой
        self.assertEqual(mh.target_name("rick-Blog-0f9e8d"), "Блог")
        self.assertEqual(mh.target_name("=rick-Blog:"), "Блог")   # точная цель tmux
        import unicodedata
        self.assertEqual(mh.target_name("=rick-БЛОГ:"), "Блог")   # регистр — тот же проект
        self.assertEqual(mh._norm(unicodedata.normalize("NFD", "Йогурт Ёж")), " йогурт еж ")   # имя с диска в NFD
        self.assertEqual(mh.project_by_name("=rick-Site:0.1"), "Site")
        self.assertEqual(mh.project_by_name("Блог"), "Blog")
        self.assertEqual(mh.target_name("rick-Blog"), "Блог")
        self.assertEqual(mh.target_name("rick-Новый"), "Новый")
        self.assertEqual(mh.match_projects("что там по лендингу и шрифту?")[0], "Site")
        self.assertIn("Blog", mh.match_projects("покажи черновики"))
        self.assertEqual(mh.match_projects("поставь будильник"), [])

    def test_projects_without_keywords_file(self):
        os.remove(os.path.join(self.home, "memory", "keywords.json"))
        self.assertEqual(set(mh.keywords()), {"Site", "Blog"})
        self.assertEqual(mh.match_projects("что по Site?"), ["Site"])


class PromptTest(HookSandbox):
    def test_prompt_journal_mail_dossier(self):
        bus.tell("Блог", "пост готов")
        bus.tell_voice("что по сайту")
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="что по лендингу? шрифт выбран?"), False)
        self.assertLess(out.index("📥 Голос"), out.index("📥 Блог"))
        self.assertIn("Проект Site", out)
        self.assertIn("## Ждёт хозяина", out)
        self.assertLessEqual(len(out), mh.LIMIT)
        self.assertEqual(bus.pending(), [])
        self.assertIn("[чел] Тестер → [агент] Рик · что по лендингу? шрифт выбран? · чат Рика", self.journal()[-1])
        t = bus.turn()
        self.assertTrue(t["busy"])
        self.assertEqual(t["author"], "Тестер")
        again = mh.prompt(self.inp("UserPromptSubmit", prompt="ещё раз про лендинг"), False)
        self.assertNotIn("Проект Site", again)  # одна выдержка — не чаще раза в 10 мин

    def test_rewake_mail_not_journaled_twice(self):
        n = len(self.journal())
        mh.prompt(self.inp("UserPromptSubmit", prompt=REWAKE), False)
        self.assertEqual(len(self.journal()), n)
        self.assertEqual(bus.turn()["author"], "Тестер (голос)")

    def test_not_rick_silent(self):
        bus.tell("Блог", "строка")
        before = len(self.journal())
        self.assertEqual(mh.prompt(self.inp("UserPromptSubmit", session_id="other", prompt="сайт"), False), "")
        self.assertEqual(mh.prompt(self.inp("UserPromptSubmit", agent_id="a1", prompt="сайт"), False), "")
        self.assertEqual(len(self.journal()), before)
        self.assertEqual(len(bus.pending()), 1)

    def test_quiet_only_journal(self):
        bus.tell("Блог", "строка")
        self.assertEqual(mh.prompt(self.inp("UserPromptSubmit", prompt=SITE), True), "")
        self.assertIn("[агент] Сайт → [агент] Рик", self.journal()[-1])
        self.assertEqual(len(bus.pending()), 1)

    def test_warnings_once_per_two_hours(self):
        sys.modules["mem_guard"] = types.SimpleNamespace(
            check=lambda home=None, now=None: ["⚠ memory/NOW.md: 5,2 КБ при потолке 4 — сжать"])
        self.assertEqual(mh.stop(self.inp("Stop", last_assistant_message="готово"), False), "")
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="привет"), False)
        self.assertIn("⚠ memory/NOW.md: 5,2 КБ", out)
        self.assertNotIn("⚠ memory/NOW.md", mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False))
        real = bus._now
        bus._now = lambda: real() + 2 * 3600 + 5
        self.assertIn("⚠ memory/NOW.md", mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False))

    def test_context_size_fact(self):
        with open(self.transcript, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 400_000, "cache_creation_input_tokens": 5_000}}}) + "\n")
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="привет"), False)
        self.assertIn("Контекст Рика 405K", out)
        self.assertNotIn("Контекст Рика", mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False))

    def test_form_skeleton_every_turn(self):
        skel = mh.SKELETON  # полный скелет — из стиля «Рик», не пишется второй раз; кладёт его только start
        with open(mh.STYLE, encoding="utf-8") as f:
            style = f.read()
        self.assertIn(skel.split("\n", 1)[1], style)
        order = ["«👾 СДЕЛАЙ»", "**✅ <вердикт", "\nСейчас:", "**1. <Тема>** —", "\n<Проект или Рик> · готово ✅",
                 "## Запреты"]
        self.assertEqual([skel.index(x) for x in order], sorted(skel.index(x) for x in order))
        self.assertNotIn("Проекта два", skel)  # число из примера модель копировала в ответ
        start = mh.start(self.inp("SessionStart", source="startup"), False)
        self.assertEqual(start.count(skel), 1)
        form = mh.FORM  # каждый ход — одна строка-напоминание: стиль и так в системном промпте
        self.assertLessEqual(len(form), 300)
        self.assertNotIn("\n", form)
        for x in ("👾 СДЕЛАЙ", "вердикт", "«Сейчас:»", "темы", "строка состояния", "только в ❓", "«уйдёт»"):
            self.assertIn(x, form)
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="привет"), False)
        self.assertTrue(out.startswith(form), out[:300])
        self.assertNotIn("## Запреты", out)
        self.assertTrue(mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False).startswith(form))  # каждый ход
        self.assertEqual(out.count(form), 1)
        self.assertEqual(mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), True), "")
        self.assertEqual(mh.prompt(self.inp("UserPromptSubmit", session_id="other", prompt="ещё"), False), "")

    def test_unanswered_word_next_turn(self):
        mh.prompt(self.inp("UserPromptSubmit", prompt="сделай мне лендинг в папке Сайт"), False)
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="ты тупой, ничего не работает"), False)  # stop не было
        self.assertIn("⚠ без ответа: «сделай мне лендинг в папке Сайт»", out)
        self.assertTrue(out.startswith(mh.FORM))
        mh.stop(self.inp("Stop", last_assistant_message="**🛑 Сломалось: лендинг не ушёл**"), True)
        self.assertNotIn("без ответа", mh.prompt(self.inp("UserPromptSubmit", prompt="ну?"), False))
        self.assertIn("⚠ без ответа: «ну?»", mh.prompt(self.inp("UserPromptSubmit", prompt=TASK), False))
        self.assertNotIn("unanswered", bus.turn())  # служебное — не слово хозяина, ответа не ждёт
        mh.prompt(self.inp("UserPromptSubmit", prompt="напомни завтра в 10"), True)  # quiet — состояние всё равно
        self.assertEqual(bus.turn().get("unanswered"), "напомни завтра в 10")
        out = mh.start(self.inp("SessionStart", source="startup"), False)  # прошлый чат умер без ответа
        self.assertIn("⚠ без ответа: «напомни завтра в 10»", out)
        self.assertNotIn("unanswered", bus.turn())


class StartTest(HookSandbox):
    def test_core_budget_and_order(self):
        for n in range(40):
            bus.tell("Блог", "строка %d %s" % (n, "x" * 200))
        bus.tell_voice("голос ждёт ожидателя")
        for n in range(20):
            bus.note("событие %d" % n)
        out = mh.start(self.inp("SessionStart", source="startup"), False)
        self.assertLessEqual(len(out), mh.LIMIT)
        self.assertIn("== memory/NOW.md ==", out)
        self.assertIn("# Сейчас", out)
        self.assertIn("== journal/, последние", out)
        self.assertIn("событие 19", out)
        self.assertLess(out.index("📥 Блог"), out.index("== memory/NOW.md =="))
        self.assertLess(out.index(mh.SKELETON), out.index("📥 Блог"))  # скелет ответа — до почты, обрезка его не съест
        self.assertNotIn("📥 Голос:", out)  # голос будит ожидатель
        self.assertEqual(len(bus.pending(("voice",))), 1)
        self.assertTrue(bus.pending(("window",)))  # не влезшее — ждёт следующего раза

    def test_wake_off_voice_comes_with_start(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"owner": "Тестер", "wake_on_mail": False}, f, ensure_ascii=False)
        bus.tell_voice("скажи, что нового")
        out = mh.start(self.inp("SessionStart", source="startup"), False)
        self.assertIn("📥 Голос: «скажи, что нового»", out)
        self.assertEqual(bus.pending(), [])

    def test_open_tasks_brief(self):
        self.assertNotIn("задач открыто", mh.start(self.inp("SessionStart", source="startup"), False))
        self.assertFalse(os.path.exists(os.path.join(self.home, "memory", "tasks.jsonl")))   # start файлов не создаёт
        mh.tasks.add([{"title": "лендинг"}])
        out = mh.start(self.inp("SessionStart", source="compact"), False)
        self.assertIn("задач открыто 1", out)
        self.assertIn("#1 ⬜ лендинг", out)

    def test_no_now_md_hints_template(self):
        os.remove(os.path.join(self.home, "memory", "NOW.md"))
        out = mh.start(self.inp("SessionStart", source="startup"), False)
        self.assertIn("memory/NOW.example.md", out)

    def test_huge_now_is_cut(self):
        with open(os.path.join(self.home, "memory", "NOW.md"), "w", encoding="utf-8") as f:
            f.write("# Сейчас\n" + ("строка ядра\n" * 3000))
        out = mh.start(self.inp("SessionStart", source="resume"), False)
        self.assertLessEqual(len(out), mh.LIMIT)
        self.assertIn("строка ядра", out)

    def test_cli_session_start_without_desktop(self):
        """JSON хука SessionStart как от Claude Code CLI (Desktop нет, роль не заявлена) → в выводе ядро памяти."""
        os.remove(bus._p("state", "rick.json"))
        sid = "33333333-4444-5555-6666-777777777777"
        raw = json.dumps({"session_id": sid, "transcript_path": self.transcript, "cwd": self.home,
                          "hook_event_name": "SessionStart", "source": "startup"})
        r, _ = self.run_hook("start", raw)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("Ядро памяти Рика", out["additionalContext"])
        self.assertIn("== memory/NOW.md ==\n# Сейчас\n- Сайт: ждёт хозяина", out["additionalContext"])
        self.assertEqual(bus.rick()["cli"], sid)
        self.assertTrue(bus.is_rick(sid))

    def test_cli_session_start_elsewhere_silent(self):
        os.remove(bus._p("state", "rick.json"))
        raw = json.dumps({"session_id": "x-1", "cwd": self.tmp.name, "hook_event_name": "SessionStart"})
        r, _ = self.run_hook("start", raw)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(os.path.exists(bus._p("state", "rick.json")))


class BatchTest(HookSandbox):
    def calls(self, *cmds):
        return [{"tool_name": "Bash", "tool_input": {"command": c}, "tool_use_id": "t%d" % i, "tool_response": "ok"}
                for i, c in enumerate(cmds)]

    def test_orders_to_journal(self):
        bus.save_turn({"busy": True, "since": time.time(), "author": "Тестер (голос)"})
        cmds = [
            'python3 "$RICK_HOME/tools/term.py" send Site "выкладывай форму" 2>&1 | tail -3',
            "python3 tools/term.py send Blog - <<'EOF'\nКоротко: допиши пост.\nEOF",
            'tmux send-keys -t rick-Site -l "проверь мобильную версию" && tmux send-keys -t rick-Site Enter',
        ]
        self.assertEqual(mh.batch(self.inp("PostToolBatch", tool_calls=self.calls(*cmds)), False), "")
        lines = self.journal()[-3:]
        want = [("Сайт", "выкладывай форму", "term.py send"), ("Блог", "Коротко: допиши пост.", "term.py send"),
                ("Сайт", "проверь мобильную версию", "tmux send-keys")]
        for line, (who, text, via) in zip(lines, want):
            self.assertIn("[агент] Рик → [агент] %s · поручение: %s" % (who, text), line)
            self.assertIn("по слову: Тестер (голос) (%s)" % via, line)

    def test_no_false_orders(self):
        n = len(self.journal())
        cmds = ["cat >> memory/NOW.md <<'EOF'\n- задача: term.py send и tmux send-keys -t rick-X\nEOF",
                "tmux send-keys -t rick-Site Enter", "ls -la", "tmux capture-pane -p", "python3 tools/term.py list"]
        mh.batch(self.inp("PostToolBatch", tool_calls=self.calls(*cmds)), False)
        self.assertEqual(len(self.journal()), n)

    def test_subagent_journal_only(self):
        cmd = 'tmux send-keys -t rick-Blog -l "сверка черновиков"'
        out = mh.batch(self.inp("PostToolBatch", agent_id="a1", agent_type="general-purpose",
                                tool_calls=self.calls(cmd)), False)
        self.assertEqual(out, "")
        self.assertIn("[агент] субагент Рика → [агент] Блог", self.journal()[-1])
        n = len(self.journal())
        mh.batch(self.inp("PostToolBatch", session_id="other", tool_calls=self.calls(cmd)), False)
        self.assertEqual(len(self.journal()), n)

    def test_voice_fact_once(self):
        now = time.time()
        bus.save_turn({"busy": True, "since": now - 60, "last": now})
        bus._write_json(bus._p("state", "voice_taken.json"), {"t": now - 52})
        out = mh.batch(self.inp("PostToolBatch", tool_calls=self.calls("ls")), False)
        self.assertRegex(out, r"^Голос хозяина ждёт 5\d с; rick_voice\.py say в этом ходе не вызывался\.$")
        self.assertEqual(mh.batch(self.inp("PostToolBatch", tool_calls=self.calls("ls")), False), "")

    def test_voice_fact_silent_after_say(self):
        now = time.time()
        bus.save_turn({"busy": True, "since": now - 60, "last": now})
        bus._write_json(bus._p("state", "voice_taken.json"), {"t": now - 52})
        out = mh.batch(self.inp("PostToolBatch", tool_calls=self.calls(
            'python3 voice/rick_voice.py say "принял, делаю" &')), False)
        self.assertEqual(out, "")
        self.assertEqual(mh.batch(self.inp("PostToolBatch", tool_calls=self.calls("ls")), False), "")


class LoopOrdersTest(unittest.TestCase):
    def test_tmux_loop_is_one_line_with_names(self):
        cmd = ('for s in rick-Site rick-Blog; do tmux send-keys -t "$s" -l "$(cat $H/$s.txt)"; done; '
               'sleep 1; for s in rick-Site rick-Blog; do tmux send-keys -t "$s" Enter; done')
        res = mh.bash_orders(cmd, {})
        self.assertEqual(len(res), 1, res)
        self.assertTrue(res[0][0].startswith("терминалы: "), res)
        self.assertNotIn("$", res[0][0] + res[0][1])

    def test_plain_send_keys_then_enter(self):
        res = mh.bash_orders('tmux send-keys -t rick-Site -l "список дел"; tmux send-keys -t rick-Site Enter', {})
        self.assertEqual([(r[1], r[2]) for r in res], [("список дел", "tmux send-keys")])


class StopTest(HookSandbox):
    def test_stop_journal_free_no_output(self):
        bus.save_turn({"busy": True, "since": time.time(), "author": "Сайт"})
        r, _ = self.run_hook("stop", json.dumps(self.inp("Stop", last_assistant_message="Готово: макеты у хозяина.",
                                                          stop_hook_active=False)))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("[агент] Рик → [чел] Тестер · ответ: Готово: макеты у хозяина. · ход на слово: Сайт",
                      self.journal()[-1])
        self.assertFalse(bus.turn()["busy"])
        self.assertEqual(bus.busy_since(), 0)

    def test_long_turn_signal(self):
        real = bus._now
        with mock.patch.object(mh, "_spawn") as popen:
            mh.prompt(self.inp("UserPromptSubmit", prompt="сделай лендинг"), False)
            mh.stop(self.inp("Stop", last_assistant_message="**✅ Готово**"), False)
            popen.assert_not_called()   # короткий ход — тихо
            mh.prompt(self.inp("UserPromptSubmit", prompt="сделай лендинг"), False)
            bus._now = lambda: real() + 90
            mh.stop(self.inp("Stop", last_assistant_message="**✅ Готово, Тестер**\nСейчас: всё."), False)
            self.assertEqual(popen.call_args[0][0][-1], "/System/Library/Sounds/Glass.aiff")
            bus._now = real
            mh.prompt(self.inp("UserPromptSubmit", prompt="и оплату"), False)
            bus._now = lambda: real() + 90
            mh.stop(self.inp("Stop", last_assistant_message="**❗ Нужно от тебя: ключ оплаты**\nСейчас: ждёт."), False)
            cmd = popen.call_args[0][0]
            self.assertTrue(cmd[1].endswith("notify.py"))
            self.assertEqual(cmd[-1], "❗ Нужно от тебя: ключ оплаты")
            popen.reset_mock()
            mh.stop(self.inp("Stop", last_assistant_message="**✅ ещё**"), False)   # без prompt (почта) — тихо
            with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"owner": "Тестер", "notify": {"long_turn": False}}, f, ensure_ascii=False)
            bus._now = real
            mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False)
            bus._now = lambda: real() + 90
            mh.stop(self.inp("Stop", last_assistant_message="**✅ ок**"), False)
            popen.assert_not_called()

    def now_md(self):
        with open(os.path.join(self.home, "memory", "NOW.md"), encoding="utf-8") as f:
            return f.read()

    def clock(self):
        """Часы хода: tasks.created и since хода — по одним часам; tick() — следующий ход позже на 10 с."""
        t = [time.time()]
        bus._now = lambda: t[0]
        p = mock.patch.object(mh.tasks, "_now", lambda: datetime.datetime.fromtimestamp(t[0]).isoformat(
            timespec="seconds"))
        p.start()
        self.addCleanup(p.stop)
        sys.modules["mem_guard"] = types.SimpleNamespace(check=lambda home=None, now=None: [])

        def tick():
            t[0] += 10
        return tick

    def test_need_answer_kept_in_memory(self):
        """❗/🛑/❓ — задача и строка «Ждёт хозяина» в этом же ходе, даже если модель их не записала; дело — слово хозяина
        этого хода, не колкий вердикт."""
        tick = self.clock()
        msg = "👾 СДЕЛАЙ: подключи Telegram\n**❗ Тестер, Телеги нет**\nСейчас: черновик готов."
        mh.prompt(self.inp("UserPromptSubmit", prompt="отправь Пете в телеграм, что встреча в 5"), True)
        mh.stop(self.inp("Stop", last_assistant_message=msg), False)
        rows = mh.tasks.load()
        self.assertEqual([(r["title"], r["project"], r["original"]) for r in rows.values()],
                         [("отправь Пете в телеграм, что встреча в 5", "Рик", "отправь Пете в телеграм, что встреча в 5")])
        self.assertIn("## Ждёт хозяина\n- ❗ отправь Пете в телеграм, что встреча в 5 (#1)\n", self.now_md())
        self.assertNotIn("Телеги нет", self.now_md())
        self.assertEqual(bus._read_json(bus._p("state", "warnings.json"))["items"], {})
        mh.stop(self.inp("Stop", last_assistant_message=msg), False)   # второй Stop того же хода — без дублей
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt="ты тупой"), True)   # та же ❗ с #1 — ни задачи, ни строки
        mh.stop(self.inp("Stop", last_assistant_message=msg.replace("нет**", "нет, #1 висит**")), False)
        self.assertEqual(len(mh.tasks.load()), 1)
        self.assertEqual(self.now_md().count("встреча в 5"), 1)
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt="сайт упал?"), True)   # строку записал сам ход — не дублировать
        mh.tasks.add([{"title": "поднять сайт"}])
        with open(os.path.join(self.home, "memory", "NOW.md"), "a", encoding="utf-8") as f:
            f.write("- сайт лежит, нужен вход в хостинг\n")
        mh.stop(self.inp("Stop", last_assistant_message="**🛑 Сайт лёг**\nСейчас: жду вход."), False)
        self.assertEqual(len(mh.tasks.load()), 2)
        self.assertNotIn("🛑", self.now_md())
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt="сделай лендинг в папке Сайт"), True)
        mh.stop(self.inp("Stop", last_assistant_message="**❓ Тестер, какой цвет**\nСейчас: жду."), False)
        self.assertEqual(mh.tasks.load()[3]["project"], "Сайт")
        self.assertIn("- ❓ сделай лендинг в папке Сайт (#3)", self.now_md())
        n = len(mh.tasks.load())
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), True)
        mh.stop(self.inp("Stop", last_assistant_message="**✅ Готово**\n**1. ❗ Тема** — x"), False)
        self.assertEqual(len(mh.tasks.load()), n)

    def test_need_answer_voice_and_template(self):
        """Слово голосом — из журнала; NOW.md из шаблона: «(пусто)» уходит, строка — в свой раздел."""
        tick = self.clock()
        with open(os.path.join(self.home, "memory", "NOW.md"), "w", encoding="utf-8") as f:
            f.write("# Сейчас\n\n## Ждёт хозяина\n- (пусто)\n\n## Проекты\n- Сайт — лендинг (#7)\n")
        bus.tell_voice("напиши Глаше, что макет готов")
        mh.prompt(self.inp("UserPromptSubmit", prompt=REWAKE), True)
        mh.stop(self.inp("Stop", last_assistant_message="**❗ Тестер, Телеги нет**"), False)
        self.assertEqual(mh.tasks.load()[1]["original"], "напиши Глаше, что макет готов")
        self.assertEqual(self.now_md(), "# Сейчас\n\n## Ждёт хозяина\n- ❗ напиши Глаше, что макет готов (#1)\n\n"
                                        "## Проекты\n- Сайт — лендинг (#7)\n")
        os.remove(os.path.join(self.home, "memory", "NOW.md"))
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt=SITE), True)   # ход на слово чата проекта — без original
        mh.stop(self.inp("Stop", last_assistant_message="**🛑 Сайт: сборка упала**"), False)
        self.assertEqual(mh.tasks.load()[2]["original"], "")
        self.assertEqual(self.now_md(), "# Сейчас\n\n## Ждёт хозяина\n- 🛑 Сайт: сборка упала (#2)\n")
        os.remove(os.path.join(self.home, "memory", "NOW.md"))
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt=SITE), True)   # слова хозяина нет — «Сейчас:», не вердикт
        mh.stop(self.inp("Stop", last_assistant_message="**❗ Тестер, опять ты**\nСейчас: нужен вход в хостинг."), False)
        self.assertEqual(mh.tasks.load()[3]["title"], "нужен вход в хостинг.")
        self.assertEqual(self.now_md(), "# Сейчас\n\n## Ждёт хозяина\n- ❗ нужен вход в хостинг. (#3)\n")
        os.remove(os.path.join(self.home, "memory", "NOW.md"))
        tick()
        mh.prompt(self.inp("UserPromptSubmit", prompt="ну что там"), True)   # задача названа — её имя, не колкость
        mh.stop(self.inp("Stop", last_assistant_message="**❗ Тестер, #3 всё висит, гений**\nСейчас: жду."), False)
        self.assertEqual(len(mh.tasks.load()), 3)
        self.assertEqual(self.now_md(), "# Сейчас\n\n## Ждёт хозяина\n- ❗ нужен вход в хостинг. (#3)\n")

    def test_broken_mem_guard_ignored(self):
        def boom(home=None, now=None):
            raise RuntimeError("упал")
        sys.modules["mem_guard"] = types.SimpleNamespace(check=boom)
        self.assertEqual(mh.stop(self.inp("Stop", last_assistant_message="ок"), False), "")


class FactsTest(HookSandbox):
    """«Факты сейчас:» кладёт хук: на короткую фразу модель инструменты не зовёт и выдумывает."""

    def test_block(self):
        out = mh.facts_block()
        self.assertTrue(out.startswith("Факты сейчас ("), out)
        for part in ("Проекты: 2 — Бот, Сайт", "Живые чаты проектов: нет", "Telegram: не подключён", "Напоминания: ",
                     "озвучка ответов: ", "разговор голосом: "):  # озвучку не путать с «слышу тебя»
            self.assertIn(part, out)
        self.assertLessEqual(len(out), mh.FACTS_MAX)
        for n in range(30):  # много проектов — строку подключений обрезка не съедает
            os.makedirs(os.path.join(self.home, "projects", "Длинный проект номер %02d" % n))
        out = mh.facts_block()
        self.assertIn("Проекты: 32 — ", out)
        self.assertIn("разговор голосом: ", out)
        self.assertLessEqual(len(out), mh.FACTS_MAX)
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"projects_root": os.path.join(self.home, "нет")}, f)
        self.assertIn("Проекты: 0 — папки проектов", mh.facts_block())

    def test_waiting_line(self):
        """Висит ❗ — строка «Ждёт хозяина: …» в фактах (сверх FACTS_MAX); пусто — строки нет."""
        self.assertNotIn("Ждёт хозяина", mh.facts_block())
        with open(os.path.join(self.home, "memory", "NOW.md"), "a", encoding="utf-8") as f:
            f.write("\n## Ждёт хозяина\n- ❗ подключить Telegram (#2)\n- ❓ цвет лендинга (#3)\n")
        out = mh.facts_block()
        line = "Ждёт хозяина: ❗ подключить Telegram (#2); ❓ цвет лендинга (#3) — последней темой ответа: «Ждёт тебя: …»"
        self.assertTrue(out.endswith("\n" + line), out)
        self.assertEqual(mh.wait_line(), line)
        mh.prompt(self.inp("UserPromptSubmit", prompt="привет"), False)   # факты показаны — строка в следующем ходе
        out = mh.prompt(self.inp("UserPromptSubmit", prompt="как дела у сайта"), False)
        self.assertNotIn("Факты сейчас", out)
        self.assertIn(line, out)   # висящее ❗ — каждый ход, не раз в 30 мин
        with open(os.path.join(self.home, "memory", "NOW.md"), "a", encoding="utf-8") as f:
            f.write("- " + "очень длинное дело " * 30 + "(#4)\n")
        self.assertTrue(mh.wait_line().endswith("… — последней темой ответа: «Ждёт тебя: …»"))
        self.assertLessEqual(len(mh.wait_line()), 260)

    def test_memory_line(self):
        """Про себя — по диску: досье, строки «что сейчас», журнал дня."""
        self.assertIn("Память: досье — 2 (Blog, Site), строк в «что сейчас» — 2, журнал за сегодня — ", mh.facts_block())
        for name in ("Site.md", "Blog.md"):
            os.remove(os.path.join(self.home, "memory", "projects", name))
        self.assertIn("Память: досье — 0 (ни одного)", mh.facts_block())

    def test_live_chat(self):
        tmux = os.path.join(self.tmp.name, "tmux")
        with open(tmux, "w") as f:
            f.write('#!/bin/sh\ncase "$1" in\n  list-sessions) printf "rick-Сайт\\t%s\\n" "$HOME";;\n'
                    '  capture-pane) echo "Do you want to proceed?";;\nesac\n')
        os.chmod(tmux, 0o755)
        with mock.patch.dict(os.environ, RICK_TMUX=tmux, RICK_CLAUDE_PROJECTS=os.path.join(self.tmp.name, "cc")):
            out = mh.facts_block()
        self.assertIn("Живые чаты проектов: Сайт (ждёт ответа)", out)

    def test_when(self):
        t = [1_000_000.0]
        bus._now = lambda: t[0]
        start = mh.start(self.inp("SessionStart", source="startup"), False)
        self.assertLess(start.index("Факты сейчас ("), start.index("== memory/NOW.md =="))
        self.assertNotIn("Факты сейчас (", mh.prompt(self.inp("UserPromptSubmit", prompt="привет"), False))
        t[0] += mh.FACTS_EVERY + 1
        self.assertIn("Факты сейчас (", mh.prompt(self.inp("UserPromptSubmit", prompt="как дела?"), False))
        self.assertNotIn("Факты сейчас (", mh.prompt(self.inp("UserPromptSubmit", prompt="ещё"), False))
        with open(os.path.join(self.home, "memory", "NOW.md"), "w", encoding="utf-8") as f:
            f.write("# Сейчас\n## Проекты\n- (пусто — Рик заполнит)\n")  # шаблон — каждый ход
        for word in ("привет", "что умеешь?"):
            self.assertIn("Проекты: 2 — Бот, Сайт", mh.prompt(self.inp("UserPromptSubmit", prompt=word), False))


class CliTest(HookSandbox):
    def test_empty_and_broken_input_silent(self):
        for mode in ("start", "prompt", "batch", "stop", "nope"):
            for raw in ("", "{битый json", "[1, 2]", json.dumps({"session_id": 5, "tool_calls": "x", "prompt": None})):
                r, _ = self.run_hook(mode, raw)
                self.assertEqual((r.returncode, r.stdout), (0, ""), (mode, raw))

    def test_json_output_and_cold_start(self):
        bus.tell("Блог", "строка для старта")
        r, dt = self.run_hook("prompt", json.dumps(self.inp("UserPromptSubmit", prompt="что по сайту?")))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("📥 Блог", out["additionalContext"])
        self.assertLess(dt, 1.0)
        r, _ = self.run_hook("prompt", json.dumps(self.inp("UserPromptSubmit", prompt="что по сайту?")), "--quiet")
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
