"""Тесты tools/term.py на подменном tmux (RICK_TMUX): настоящий tmux и claude не запускаются."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)
import term  # noqa: E402

# Подменный tmux: состояние сессий в JSON; Enter переносит поле ввода в ленту, цифра закрывает диалог.
FAKE = r'''import json, os, sys
path = os.environ["FAKE_TMUX_STATE"]
st = json.load(open(path)) if os.path.exists(path) else {"sessions": {}, "log": []}
a = sys.argv[1:]
st["log"].append(a)
code, out = 0, ""
def target():
    return a[a.index("-t") + 1].lstrip("=").rstrip(":")
ses = st["sessions"]
def get():   # как настоящий tmux: сессии нет — его сырая ошибка в stderr
    if target() not in ses:
        sys.stderr.write("no server running on /tmp/tmux-0/default\n" if not ses else "can't find session: %s\n" % target())
        sys.exit(1)
    return ses[target()]
if a[0] == "has-session":
    code = 0 if target() in ses else 1
elif a[0] == "new-session" and os.environ.get("FAKE_DIES"):
    pass   # claude вышел сразу — сессия закрылась вместе с ним
elif a[0] == "new-session":
    screen = os.environ.get("FAKE_SCREEN", "")
    ses[a[a.index("-s") + 1]] = {"path": a[a.index("-c") + 1], "pane": screen.split("\n") if screen else [], "input": ""}
elif a[0] == "kill-session":
    get()
    ses.pop(target())
elif a[0] == "list-sessions":
    out = "".join("%s\t%s\n" % (n, s["path"]) for n, s in ses.items())
elif a[0] == "capture-pane":
    s = get()
    out = "\n".join(s["pane"] + ["-" * 40, "> " + s["input"], "-" * 40]) + "\n"
elif a[0] == "send-keys":
    s = get()
    rest = a[a.index("-t") + 2:]
    if rest[0] == "-l":
        s["input"] += rest[-1]
    elif rest[0] == "Enter" and not os.environ.get("FAKE_DEAF"):
        if s["input"]:
            s["pane"].append("> " + s["input"])
        s["input"] = ""
    elif rest[0].isdigit():
        s["pane"] = ["(диалог закрыт)"]
json.dump(st, open(path, "w"), ensure_ascii=False)
sys.stdout.write(out)
sys.exit(code)
'''

TRUST = "Is this a project you created or one you trust?\n❯ 1. Yes, proceed\n  2. No, exit"
BYPASS = "WARNING: Claude Code running in Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept"
AUTO = ("Enable auto mode?\nAuto mode lets Claude handle permission prompts automatically\n"
        "❯ 1. Yes, and make it my default mode\n  2. Yes, enable auto mode\n  3. No, exit")
ASK = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No"


def slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TermTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home, self.root = os.path.join(t, "rick"), os.path.join(t, "projects")
        os.makedirs(os.path.join(self.root, "Demo.app"))
        os.makedirs(self.home)
        fake = os.path.join(t, "faketmux")
        with open(fake, "w") as fh:
            fh.write("#!%s\n%s" % (sys.executable, FAKE))
        os.chmod(fake, 0o755)
        self.state = os.path.join(t, "tmux.json")
        self.env = mock.patch.dict(os.environ, {
            "RICK_HOME": self.home, "RICK_TMUX": fake, "RICK_CLAUDE": "/bin/claude-stub",
            "RICK_CLAUDE_PROJECTS": os.path.join(t, "cc"), "RICK_TERM_WAIT": "0", "FAKE_TMUX_STATE": self.state})
        self.env.start()
        for k in ("FAKE_SCREEN", "FAKE_DEAF", "FAKE_DIES"):
            os.environ.pop(k, None)
        self.config({})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def config(self, extra):
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump(dict({"projects_root": self.root}, **extra), fh)

    def run_term(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = term.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def tmux_state(self):
        return json.loads(slurp(self.state))

    def save_state(self, st):
        with open(self.state, "w") as fh:
            json.dump(st, fh)

    def test_up_default_config(self):
        task = os.path.join(self.tmp.name, "task.txt")
        with open(task, "w") as fh:
            fh.write("Сделай отчёт.\n")
        code, out = self.run_term("up", "Demo.app", "--task", task)
        self.assertEqual(code, 0, out)
        st = self.tmux_state()
        self.assertIn("rick-Demo_app", st["sessions"])
        new = next(a for a in st["log"] if a[0] == "new-session")
        self.assertIn("RICK_HOME=" + self.home, new)
        self.assertEqual(new[new.index("-c") + 1], os.path.join(self.root, "Demo.app"))
        launch = slurp(os.path.join(self.home, "state", "launch-rick-Demo_app.sh"))
        self.assertIn("--effort high", launch)
        self.assertNotIn("--model", launch)
        self.assertNotIn("--permission-mode", launch)
        lines = slurp(os.path.join(self.home, "state", "task-rick-Demo_app.txt")).splitlines()
        self.assertIn("python3 %s/tools/rick_bus.py tell --from Demo.app " % self.home, lines[0])
        self.assertIn("прочитай памятку чата проекта: %s/docs/project-chat.md" % self.home, lines[0])
        self.assertIn("--name rick-Demo_app", launch)
        self.assertEqual(lines[1], "Сделай отчёт.")
        code, out = self.run_term("up", "Demo.app")
        self.assertIn("уже есть", out)

    def test_up_writes_memory(self):
        """Поднятый чат — строка проекта в NOW.md и досье из шаблона; повторный up строку меняет, досье не трогает."""
        os.makedirs(os.path.join(self.home, "memory"))
        with open(os.path.join(self.home, "memory", "NOW.md"), "w", encoding="utf-8") as fh:
            fh.write("# Сейчас\n\n## Проекты\n- (пусто — Рик заполнит)\n\n## Ждёт хозяина\n- (пусто)\n")
        import tasks
        tasks.add([{"title": "лендинг", "project": "Demo.app", "original": "сделай лендинг"}])
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 0, out)
        now = slurp(os.path.join(self.home, "memory", "NOW.md"))
        self.assertIn("## Проекты\n- Demo.app — лендинг (#1)\n\n## Ждёт хозяина\n- (пусто)", now)
        dossier = os.path.join(self.home, "memory", "projects", "Demo.app.md")
        self.assertIn("## Сейчас\n- лендинг (#1)", slurp(dossier))
        with open(dossier, "a", encoding="utf-8") as fh:
            fh.write("- своя строка\n")
        self.run_term("stop", "Demo.app")
        tasks.update(1, "done")
        task = os.path.join(self.tmp.name, "task.txt")
        with open(task, "w") as fh:
            fh.write("сделай отчёт в папке Demo.app\n")
        self.assertEqual(self.run_term("up", "Demo.app", "--task", task)[0], 0)
        now = slurp(os.path.join(self.home, "memory", "NOW.md"))
        self.assertIn("- Demo.app — сделай отчёт в папке Demo.app\n", now)
        self.assertEqual(now.count("Demo.app — "), 1)
        self.assertIn("- своя строка", slurp(dossier))

    def test_up_model_and_bypass_by_path(self):
        self.config({"model": "some-model", "permission_mode": "bypassPermissions", "effort": "medium"})
        code, out = self.run_term("up", os.path.join(self.root, "Demo.app"), "--effort", "low")
        self.assertEqual(code, 0, out)
        launch = slurp(os.path.join(self.home, "state", "launch-rick-Demo_app.sh"))
        self.assertIn("--model some-model --effort low --permission-mode bypassPermissions", launch)

    def test_missing_project(self):
        code, out = self.run_term("up", "Nope")
        self.assertEqual(code, 2)
        self.assertIn("папки", out)

    def test_trust_dialog_own_folder_auto(self):
        os.environ["FAKE_SCREEN"] = TRUST
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 0, out)
        self.assertIn("подтвердил: доверие к папке", out)
        self.assertIn(["send-keys", "-t", "=rick-Demo_app:", "1"], self.tmux_state()["log"])

    def test_trust_dialog_outside_root_needs_config(self):
        other = os.path.join(self.tmp.name, "elsewhere", "Side")
        os.makedirs(other)
        os.environ["FAKE_SCREEN"] = TRUST
        code, out = self.run_term("up", other)
        self.assertEqual(code, 2)
        self.assertIn("term_confirm_dialogs", out)
        self.config({"term_confirm_dialogs": True})
        os.environ["FAKE_SCREEN"] = ""
        # сессия уже есть, диалог снова на экране: его разбирает send
        st = self.tmux_state()
        st["sessions"]["rick-Side"]["pane"] = TRUST.split("\n")
        self.save_state(st)
        code, out = self.run_term("send", other, "привет")
        self.assertEqual(code, 0, out)
        self.assertIn(["send-keys", "-t", "=rick-Side:", "1"], self.tmux_state()["log"])

    def test_bypass_dialog_refused_without_bypass_mode(self):
        self.config({"term_confirm_dialogs": True})
        os.environ["FAKE_SCREEN"] = BYPASS
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 2)
        self.assertIn("не подтверждаю", out)
        self.config({"term_confirm_dialogs": True, "permission_mode": "bypassPermissions"})
        st = self.tmux_state()
        del st["sessions"]["rick-Demo_app"]
        self.save_state(st)
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 0, out)
        self.assertIn("подтвердил: bypass", out)
        self.assertIn(["send-keys", "-t", "=rick-Demo_app:", "2"], self.tmux_state()["log"])

    def test_auto_dialog_needs_auto_mode(self):
        """«Enable auto mode?» — не bypass и не «Do you want»: свой вид, жмём «Yes, enable auto mode», не «default»."""
        os.environ["FAKE_SCREEN"] = AUTO
        for cfg, why in (({}, "term_confirm_dialogs"), ({"term_confirm_dialogs": True}, "не подтверждаю"),
                         ({"term_confirm_dialogs": True, "permission_mode": "bypassPermissions"}, "не подтверждаю")):
            self.config(cfg)
            if os.path.exists(self.state):
                self.save_state({"sessions": {}, "log": []})
            code, out = self.run_term("up", "Demo.app")
            self.assertEqual(code, 2, cfg)
            self.assertIn(why, out, cfg)
            self.assertIn("auto", out, cfg)
        self.config({"term_confirm_dialogs": True, "permission_mode": "auto"})
        self.save_state({"sessions": {}, "log": []})
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 0, out)
        self.assertIn("подтвердил: auto", out)
        log = self.tmux_state()["log"]
        self.assertIn(["send-keys", "-t", "=rick-Demo_app:", "2"], log)
        self.assertNotIn(["send-keys", "-t", "=rick-Demo_app:", "1"], log)

    def put_pane(self, sess, text):
        st = self.tmux_state()
        st["sessions"][sess]["pane"] = text.split("\n")
        self.save_state(st)

    def test_watch_finds_question_once_and_times_out(self):
        os.makedirs(os.path.join(self.root, "Блог"))
        self.run_term("up", "Demo.app")
        self.run_term("up", "Блог")
        code, out = self.run_term("watch", "--max", "0")
        self.assertEqual(code, 3, out)
        for screen in (ASK, TRUST, BYPASS, AUTO):
            self.put_pane("rick-Блог", "старая строка\n" * 20 + screen)
            code, out = self.run_term("watch", "--max", "0")
            self.assertEqual(code, 0, screen)
            self.assertIn("ВОПРОС в rick-Блог:", out)
            self.assertNotIn("rick-Demo_app", out)
            self.assertLessEqual(len(out.strip().splitlines()), 9)   # заголовок и 8 строк хвоста
            self.assertEqual(self.run_term("watch", "--max", "0")[0], 3, screen)   # тот же вопрос второй раз не будит
        self.put_pane("rick-Блог", "работаю")
        self.assertEqual(self.run_term("watch", "--max", "0")[0], 3)
        self.put_pane("rick-Блог", ASK)   # ответили, потом спросили снова — будит
        self.assertEqual(self.run_term("watch", "--max", "0")[0], 0)

    def test_list_marks_waiting_chat(self):
        os.makedirs(os.path.join(self.root, "Блог"))
        self.run_term("up", "Demo.app")
        self.run_term("up", "Блог")
        self.put_pane("rick-Блог", ASK)
        code, out = self.run_term("list")
        self.assertEqual(code, 0)
        rows = dict(line.split("  ", 1) for line in out.splitlines())
        self.assertIn("⚠ ждёт ответа", rows["rick-Блог"])
        self.assertNotIn("⚠", rows["rick-Demo_app"])

    def test_send_text_then_enter_and_read(self):
        self.run_term("up", "Demo.app")
        code, out = self.run_term("send", "Demo.app", "Проверь тесты\x1b[A")
        self.assertEqual(code, 0, out)
        self.assertIn("дошло", out)
        log = self.tmux_state()["log"]
        i = log.index(["send-keys", "-t", "=rick-Demo_app:", "-l", "--", "Проверь тесты[A"])
        self.assertEqual(log[i + 1], ["send-keys", "-t", "=rick-Demo_app:", "Enter"])
        code, out = self.run_term("read", "Demo.app", "-n", "5")
        self.assertIn("> Проверь тесты", out)

    def test_send_not_delivered(self):
        self.run_term("up", "Demo.app")
        os.environ["FAKE_DEAF"] = "1"
        code, out = self.run_term("send", "Demo.app", "Сделай")
        self.assertEqual(code, 1)
        self.assertIn("в поле", out)
        self.assertEqual(self.run_term("send", "Ghost", "x")[0], 2)

    def test_list_context_and_idle(self):
        self.run_term("up", "Demo.app")
        folder = os.path.join(self.root, "Demo.app")
        cc = os.path.join(os.environ["RICK_CLAUDE_PROJECTS"], "".join(c if c.isalnum() else "-" for c in folder))
        os.makedirs(cc)
        usage = {"input_tokens": 1000, "cache_read_input_tokens": 300000, "cache_creation_input_tokens": 1000}
        with open(os.path.join(cc, "a.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 5}}}) + "\n")
            fh.write("не json\n")
        old = time.time() - 600
        os.utime(os.path.join(cc, "a.jsonl"), (old, old))
        code, out = self.run_term("list")
        self.assertEqual(code, 0)
        self.assertIn("rick-Demo_app  302K  простой 10 мин", out)
        self.assertIn(">300K", out)

    def test_projects_and_session_names(self):
        code, out = self.run_term("projects")
        self.assertEqual(out.strip(), "Demo.app")
        self.assertEqual(term.session("rick-My.Proj:1"), "rick-My_Proj_1")
        self.assertEqual(term.session("Мой сайт"), "rick-Мой_сайт")

    def test_cyrillic_projects_same_length_apart(self):
        for name in ("Сайт", "Блог"):
            os.makedirs(os.path.join(self.root, name))
            code, out = self.run_term("up", name)
            self.assertEqual(code, 0, out)
        self.assertTrue({"rick-Сайт", "rick-Блог"} <= set(self.tmux_state()["sessions"]))
        self.run_term("send", "Блог", "пост")
        self.assertIn("> пост", self.run_term("read", "Блог")[1])
        self.assertNotIn("> пост", self.run_term("read", "Сайт")[1])

    def test_name_taken_by_other_folder(self):
        os.makedirs(os.path.join(self.root, "Demo_app"))
        self.run_term("up", "Demo.app")
        code, out = self.run_term("send", "Demo_app", "чужому")
        self.assertEqual(code, 2, out)   # чат Demo_app не поднят — в чат Demo.app не шлём
        self.assertNotIn("> чужому", self.run_term("read", "Demo.app")[1])
        code, out = self.run_term("up", "Demo_app")
        self.assertEqual(code, 0, out)
        sess = [s for s in self.tmux_state()["sessions"] if s.startswith("rick-Demo_app-")]
        self.assertEqual(len(sess), 1, out)
        self.assertEqual(self.run_term("send", "Demo_app", "своему")[0], 0)
        self.assertIn("> своему", self.run_term("read", "Demo_app")[1])

    def test_missing_folder_never_reaches_other_folder_chat(self):
        """Папки «Demo_app» нет, а rick-Demo_app — чат живой папки Demo.app: по имени туда не шлём и не гасим."""
        self.run_term("up", "Demo.app")
        for argv in (("send", "Demo_app", "чужому"), ("read", "Demo_app"), ("stop", "Demo_app")):
            code, out = self.run_term(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("чата этой папки нет", out)
        self.assertNotIn("> чужому", self.run_term("read", "Demo.app")[1])
        self.assertEqual(self.sessions(), ["rick-Demo_app"])
        # папку чата удалили — погасить его по имени можно
        gone = os.path.join(self.root, "Gone")
        os.makedirs(gone)
        self.run_term("up", "Gone")
        os.rmdir(gone)
        self.assertEqual(self.run_term("send", "Gone", "живой?")[0], 0)
        self.assertEqual(self.run_term("stop", "Gone")[0], 0)
        self.assertEqual(self.sessions(), ["rick-Demo_app"])

    def test_hashed_chat_survives_base_death(self):
        os.makedirs(os.path.join(self.root, "Demo_app"))
        self.run_term("up", "Demo.app")
        self.run_term("up", "Demo_app")
        hashed = [s for s in self.tmux_state()["sessions"] if s.startswith("rick-Demo_app-")]
        self.assertEqual(len(hashed), 1)
        st = self.tmux_state()
        del st["sessions"]["rick-Demo_app"]   # tmux kill-session -t rick-Demo_app
        self.save_state(st)
        code, out = self.run_term("send", "Demo_app", "живой")
        self.assertEqual(code, 0, out)
        self.assertIn(hashed[0], out)
        code, out = self.run_term("up", "Demo_app")
        self.assertIn("уже есть " + hashed[0], out)
        self.assertEqual(sorted(self.tmux_state()["sessions"]), hashed)
        # базовое имя свободно, но чат папки Demo.app поднимается под ним, а не в хешевом
        self.assertEqual(self.run_term("up", "Demo.app")[0], 0)
        self.assertEqual(sorted(self.tmux_state()["sessions"]), sorted(hashed + ["rick-Demo_app"]))
        self.assertNotIn("> живой", self.run_term("read", "Demo.app")[1])
        self.assertIn("> живой", self.run_term("read", "Demo_app")[1])

    def test_from_quoted_and_reply_allowed(self):
        os.makedirs(os.path.join(self.root, "My Site"))
        code, out = self.run_term("up", "My Site")
        self.assertEqual(code, 0, out)
        task = slurp(os.path.join(self.home, "state", "task-rick-My_Site.txt"))
        self.assertIn("tell --from 'My Site' ", task)
        launch = slurp(os.path.join(self.home, "state", "launch-rick-My_Site.sh"))
        perms = os.path.join(self.home, "state", "settings-rick-My_Site.json")
        self.assertIn("--settings " + perms, launch)
        self.assertLess(launch.index("--settings"), launch.index(" -- \"$(cat"))
        allow = json.loads(slurp(perms))["permissions"]["allow"]
        self.assertEqual(allow[0], "Bash(python3 %s/tools/rick_bus.py tell *)" % self.home)
        self.assertIn('Bash(python3 "$RICK_HOME/tools/rick_bus.py" tell *)', allow)

    def test_reply_rule_matches_task_with_odd_home(self):
        odd = os.path.join(self.tmp.name, "Мой Рик (копия)")
        os.makedirs(odd)
        with open(os.path.join(odd, "config.json"), "w") as fh:
            json.dump({"projects_root": self.root}, fh)
        os.environ["RICK_HOME"] = odd
        self.assertEqual(self.run_term("up", "Demo.app")[0], 0)
        cmd = "python3 '%s/tools/rick_bus.py' tell" % odd
        task = slurp(os.path.join(odd, "state", "task-rick-Demo_app.txt"))
        self.assertIn(cmd + " --from Demo.app ", task)
        allow = json.loads(slurp(os.path.join(odd, "state", "settings-rick-Demo_app.json")))["permissions"]["allow"]
        want = "Bash(python3 '%s/tools/rick_bus.py' tell *)" % odd.replace("(", "\\(").replace(")", "\\)")
        self.assertEqual(allow[0], want)
        launch = slurp(os.path.join(odd, "state", "launch-rick-Demo_app.sh"))
        self.assertNotIn("--allowedTools", launch)

    def sessions(self):
        return sorted(self.tmux_state()["sessions"])

    def test_case_nfd_and_link_find_same_chat(self):
        """APFS не различает регистр и NFD/NFC: та же папка — тот же чат, второй не поднимается."""
        self.assertEqual(self.run_term("up", "Demo.app")[0], 0)
        os.makedirs(os.path.join(self.root, "Йогурт"))
        link = os.path.join(self.tmp.name, "ссылка")
        os.symlink(os.path.join(self.root, "Demo.app"), link)
        self.assertIn("> по ссылке", self.run_term("send", link, "по ссылке")[1] + self.run_term("read", "Demo.app")[1])
        if not os.path.isdir(os.path.join(self.root, "DEMO.APP")):
            self.skipTest("файловая система различает регистр и NFD/NFC")
        code, out = self.run_term("up", unicodedata.normalize("NFD", "Йогурт"))
        self.assertEqual(code, 0, out)
        self.assertIn("rick-Йогурт", self.sessions())   # имя сессии — NFC, как на диске
        task = slurp(os.path.join(self.home, "state", "task-rick-Йогурт.txt"))
        self.assertIn("--from 'Йогурт' ", task)
        for typed in ("demo.app", "DEMO.APP", unicodedata.normalize("NFD", "ЙОГУРТ")):
            code, out = self.run_term("up", typed)
            self.assertIn("уже есть", out, typed)
            self.assertEqual(self.run_term("send", typed, "регистр")[0], 0, typed)
        self.assertEqual(self.sessions(), ["rick-Demo_app", "rick-Йогурт"])

    def test_stop_kills_own_chat_only(self):
        os.makedirs(os.path.join(self.root, "Demo_app"))
        self.run_term("up", "Demo.app")
        self.run_term("up", "Demo_app")
        hashed = [s for s in self.sessions() if s.startswith("rick-Demo_app-")]
        code, out = self.run_term("stop", "Demo_app")   # у Demo_app хеш-имя: '=rick-Demo_app' убил бы чужой
        self.assertEqual(code, 0, out)
        self.assertIn("погасил " + hashed[0], out)
        self.assertEqual(self.sessions(), ["rick-Demo_app"])
        self.assertIn("уже есть rick-Demo_app — сменить: term.py stop Demo.app", self.run_term("up", "Demo.app")[1])
        self.assertEqual(self.run_term("stop", "Demo.app")[0], 0)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.run_term("stop", "Demo.app")[0], 2)

    def test_trust_not_auto_with_repo_claude_settings(self):
        """Клонированный репозиторий со своими хуками и MCP: доверие — только по term_confirm_dialogs."""
        self.assertEqual(term.CLAUDE_FILES, (".claude", ".mcp.json"))
        # любое содержимое .claude: скилл с allowed-tools, хуки в settings, пустая папка — уже риск
        for f in (".claude/skills/x/SKILL.md", ".claude/settings.json", ".claude/settings.local.json",
                  ".claude/agents/a.md", ".claude/commands/c.md", ".claude/", ".mcp.json"):
            st = self.tmux_state() if os.path.exists(self.state) else None
            if st:
                st["sessions"].clear()
                self.save_state(st)
            path = os.path.join(self.root, "Demo.app", f)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not f.endswith("/"):
                with open(path, "w") as fh:
                    fh.write("---\nallowed-tools: Bash\n---\n" if f.endswith(".md") else "{}")
            os.environ["FAKE_SCREEN"] = TRUST
            self.config({})
            code, out = self.run_term("up", "Demo.app")
            self.assertEqual(code, 2, f)
            self.assertIn(f.split("/")[0], out)
            self.assertNotIn(["send-keys", "-t", "=rick-Demo_app:", "1"], self.tmux_state()["log"])
            shutil.rmtree(os.path.join(self.root, "Demo.app", ".claude"), ignore_errors=True)
            if f == ".mcp.json":
                os.remove(path)
        with open(os.path.join(self.root, "Demo.app", ".mcp.json"), "w") as fh:
            fh.write("{}")
        st = self.tmux_state()
        st["sessions"].clear()
        self.save_state(st)
        self.config({"term_confirm_dialogs": True})
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 0, out)
        self.assertIn("подтвердил: доверие к папке", out)

    def test_closed_chat_gives_plain_error_not_raw_tmux(self):
        """Класс: сессия tmux исчезла — понятная причина и выход 2, без «no server running» и «can't find»."""
        os.environ["FAKE_DIES"] = "1"
        code, out = self.run_term("up", "Demo.app")
        self.assertEqual(code, 2, out)
        self.assertIn("чат проекта закрылся сразу после запуска — похоже, Claude не вошёл в аккаунт: открой "
                      "Терминал и набери claude", out)
        self.assertNotIn("tmux", out)
        os.environ.pop("FAKE_DIES")
        os.environ["FAKE_SCREEN"] = TRUST   # закрылся посреди диалога первого запуска — та же причина
        with mock.patch.object(term, "dialogs", side_effect=term.Gone(term.GONE)):
            code, out = self.run_term("up", "Demo.app")
        self.assertEqual((code, "набери claude" in out), (2, True), out)
        os.environ.pop("FAKE_SCREEN")
        for alone in (True, False):   # пустой сервер и чужие сессии живы — разные сырые ошибки tmux
            st = self.tmux_state()
            st["sessions"] = {} if alone else {"rick-Other": {"path": self.root, "pane": [], "input": ""}}
            self.save_state(st)
            with mock.patch.object(term, "alive", return_value=True):   # погас между проверкой и делом
                for argv in (("read", "Demo.app"), ("send", "Demo.app", "привет"), ("stop", "Demo.app")):
                    code, out = self.run_term(*argv)
                    self.assertEqual(code, 2, argv)
                    self.assertIn("чат закрылся", out)
                    self.assertNotIn("no server", out)
                    self.assertNotIn("can't find", out)
        with mock.patch.object(term, "sessions", return_value={"rick-Demo_app": self.root}):
            code, out = self.run_term("list")   # list-sessions видел чат, capture — уже нет
        self.assertEqual(code, 0, out)
        self.assertNotIn("can't find", out)


if __name__ == "__main__":
    unittest.main()
