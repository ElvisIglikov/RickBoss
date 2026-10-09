"""Данные хозяина не живут в git: память — из шаблонов, люди Telegram — из team_config.json."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "telethon_kit"))
import send_hooks  # noqa: E402


class TeamConfigTest(unittest.TestCase):
    def test_aliases_and_never_send_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "team_config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"aliases": {"Ivan": 111, "плохо": "x"}, "never_send": {"222": "рабочий аккаунт"}}, fh)
            with mock.patch.object(send_hooks, "CONFIG_PATH", path):
                self.assertEqual(send_hooks._ids("aliases"), {"ivan": 111})
                self.assertEqual(send_hooks._ids("never_send"), {222: "рабочий аккаунт"})
            with mock.patch.object(send_hooks, "CONFIG_PATH", os.path.join(tmp, "нет.json")):
                self.assertEqual(send_hooks._ids("aliases"), {})

    def test_code_ships_empty(self):
        if os.path.exists(send_hooks.CONFIG_PATH):   # иначе «прошёл» значило бы «ничего не проверил»
            self.skipTest("есть свой team_config.json — пустоту кода проверить нельзя")
        self.assertEqual((send_hooks.ALIASES, send_hooks.NEVER_SEND_TELEGRAM_IDS), ({}, {}))


@unittest.skipUnless(shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git")), "нужен git-клон")
class GitIgnoreTest(unittest.TestCase):
    def test_user_files_ignored_templates_not(self):
        user = ["config.json", ".env", "voice/.env", "voice/config.json", "telethon_kit/.env",
                "telethon_kit/team_config.json", "telethon_kit/user_session.session", "memory/NOW.md",
                "memory/people.md", "memory/keywords.json", "memory/projects/A.md", "journal/2026-01-01.md",
                "state/rick.json", "inbox/queue.jsonl", "live/log/server.log", "voice/cache/a.mp3",
                "voice/otvety/spisok.txt", "telethon_kit/.telegram_safety/quota.json"]
        for rel in user:
            r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--no-index", rel])
            self.assertEqual(r.returncode, 0, "не в .gitignore: " + rel)
        for rel in ("memory/NOW.example.md", "memory/people.example.md", "telethon_kit/team_config.example.json"):
            r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--no-index", rel])
            self.assertEqual(r.returncode, 1, "шаблон в .gitignore: " + rel)


if __name__ == "__main__":
    unittest.main()
