"""Тесты tools/remind.py (напоминание в «Напоминания» Мака) и tools/rick_config.py status. Мак не трогаем:
osascript подменён скриптом, который пишет, что ему передали."""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)
import remind  # noqa: E402

PY = sys.executable
FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_OUT"
cat > "$FAKE_OUT.script"
[ -n "$FAKE_ERR" ] && { echo "$FAKE_ERR" >&2; exit 1; }
echo ok
"""


class RemindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "osascript")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        os.chmod(self.fake, 0o700)
        self.out = os.path.join(self.tmp.name, "args")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, err=""):
        env = dict(os.environ, RICK_OSASCRIPT=self.fake, FAKE_OUT=self.out, FAKE_ERR=err)
        return subprocess.run([PY, os.path.join(TOOLS, "remind.py"), *args], env=env, capture_output=True,
                              text=True, timeout=20)

    def test_exact_time_not_now_plus_day(self):
        at = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=10, minute=0)
        r = self.run_cli('позвонить бухгалтеру «срочно»', "--at", at.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.out) as f:
            args = f.read().splitlines()
        self.assertEqual(args, ["-", "позвонить бухгалтеру «срочно»", str(at.year), str(at.month), str(at.day),
                                "36000"])
        with open(self.out + ".script") as f:
            self.assertIn('application "Reminders"', f.read())
        self.assertIn("в 10:00", r.stdout)
        self.assertIn("Напомнит Мак", r.stdout)

    def test_bad_and_past_time(self):
        r = self.run_cli("x", "--at", "завтра в 10")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ГГГГ-ММ-ДД ЧЧ:ММ", r.stderr)
        r = self.run_cli("x", "--at", "2001-01-01 10:00")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("уже прошло", r.stderr)
        self.assertFalse(os.path.exists(self.out))  # Мак не звали

    def test_denied_says_what_to_click(self):
        at = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%d 09:30")
        r = self.run_cli("x", "--at", at, err="execution error: Not authorized to send Apple events (-1743)")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Автоматизация", r.stderr)
        self.assertNotIn("osascript", r.stderr)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "rick")
        os.makedirs(os.path.join(self.home, "telethon_kit"))
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.projects, "Сайт"))
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"owner": "Макс", "projects_root": self.projects}, f, ensure_ascii=False)

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "ELEVENLABS", "OPENAI"))}
        env.update(RICK_HOME=self.home, LIVE_PORT="1")
        r = subprocess.run([PY, os.path.join(TOOLS, "rick_config.py"), "status"], env=env, capture_output=True,
                           text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_nothing_connected(self):
        out = self.status()
        self.assertIn("Хозяин: Макс", out)
        self.assertIn("папок 1", out)
        self.assertIn("Telegram-бот (файлы хозяину): не подключён", out)
        self.assertIn("Telegram от твоего аккаунта: не подключён — нет: установка", out)
        self.assertIn("wake_on_mail выключен", out)

    def test_bot_connected_values_never_printed(self):
        with open(os.path.join(self.home, ".env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=123:secretTOKENvalue\nTELEGRAM_CHAT_ID=987654321\n")
        out = self.status()
        self.assertIn("Telegram-бот (файлы хозяину): подключён", out)
        self.assertNotIn("secretTOKENvalue", out)
        self.assertNotIn("987654321", out)


if __name__ == "__main__":
    unittest.main()
