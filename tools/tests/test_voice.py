"""Тесты голоса (voice/rick_voice.py) без звука: subprocess подменён, всё — во временной папке RICK_HOME."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_voice(home):
    os.environ["RICK_HOME"] = home
    spec = importlib.util.spec_from_file_location("rick_voice_t", os.path.join(ROOT, "voice", "rick_voice.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.old_home = os.environ.get("RICK_HOME")
        self.old_key = os.environ.pop("ELEVENLABS_API_KEY", None)
        self.v = load_voice(self.home)
        self.popen = mock.patch.object(self.v.subprocess, "Popen").start()
        self.run = mock.patch.object(self.v.subprocess, "run").start()
        self.net = mock.patch.object(self.v, "request", side_effect=AssertionError("сеть в тесте")).start()

    def tearDown(self):
        mock.patch.stopall()
        if self.old_home is None:
            os.environ.pop("RICK_HOME", None)
        else:
            os.environ["RICK_HOME"] = self.old_home
        if self.old_key is not None:
            os.environ["ELEVENLABS_API_KEY"] = self.old_key
        self.tmp.cleanup()

    def write_config(self, data):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_say_uses_system_voice_from_config(self):
        self.write_config({"voice": {"say_voice": "Yuri", "say_rate": "190"}})
        self.v.cmd_say("Готово. Тесты **зелёные**: `tools/term.py` [#12]")
        args = self.popen.call_args[0][0]
        self.assertEqual(args[:5], ["say", "-v", "Yuri", "-r", "190"])
        self.assertIn("Готово. Тесты зелёные", args[5])
        self.assertNotIn("term.py", args[5])
        self.assertNotIn("#12", args[5])

    def test_paragraphs_get_pause(self):
        self.v.cmd_say("Раз.\n\nДва.")
        self.assertIn("[[slnc 1200]]", self.popen.call_args[0][0][-1])

    def test_empty_text_is_silent(self):
        self.v.cmd_say("```код``` `/tmp/x.py`")
        self.popen.assert_not_called()

    def test_live_front_writes_answer_instead_of_speaking(self):
        os.makedirs(os.path.join(self.home, "voice", "cache"))
        open(os.path.join(self.home, "voice", "cache", ".live_front"), "w").close()
        self.v.cmd_say("Первое.\n\nВторое.")
        self.popen.assert_not_called()
        with open(os.path.join(self.home, "voice", "otvety", "spisok.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "live\t \tПервое. Второе.\n")

    def test_elevenlabs_without_key_falls_back_to_say(self):
        self.write_config({"voice": {"engine": "elevenlabs"}})
        self.v.cmd_say("Привет")
        self.net.assert_not_called()
        self.assertEqual(self.popen.call_args[0][0][0], "say")

    def test_elevenlabs_network_error_falls_back_to_say(self):
        self.write_config({"voice": {"engine": "elevenlabs"}})
        os.makedirs(os.path.join(self.home, "voice"))
        with open(os.path.join(self.home, "voice", ".env"), "w") as f:
            f.write("ELEVENLABS_API_KEY=test\n")
        with open(os.path.join(self.home, "voice", "config.json"), "w") as f:
            json.dump({"voice_id": "v1"}, f)
        self.net.side_effect = OSError("нет сети")
        with mock.patch("sys.stderr"):
            self.v.cmd_say("Привет")
        self.net.assert_called_once()
        self.assertEqual(self.popen.call_args[0][0][0], "say")

    def test_use_switches_profile_without_key(self):
        with mock.patch("builtins.print"):
            self.v.cmd_use("babka")
            self.v.cmd_next()
        with open(os.path.join(self.home, "voice", "config.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["current"], list(self.v.load_profiles())[5])

    def test_profiles_have_required_fields(self):
        profiles = self.v.load_profiles()
        self.assertTrue(profiles, "профилей нет — проверять нечего")
        for alias, p in profiles.items():
            for field in ("name", "prompt", "text", "manner", "hint"):
                self.assertTrue(p.get(field), f"{alias}: нет {field}")


    def test_stop_patterns_are_literal(self):
        with mock.patch.object(self.v, "DATA", "/tmp/a.b(c)/voice"):
            self.v.cmd_stop()
        pats = [c.args[0][2] for c in self.run.call_args_list if c.args[0][0] == "pkill"]
        self.assertIn(r"afplay .*/tmp/a\.b\(c\)/voice", pats)
        self.assertFalse([p for p in pats if "say" in p], pats)   # чужие say с тем же голосом не трогаем

    def test_stop_kills_own_say_by_pid(self):
        self.popen.return_value.pid = 4242
        self.v.cmd_say("Привет.")
        with open(self.v.SAY_PID) as f:
            self.assertEqual(f.read(), "4242")
        self.run.return_value.stdout = "/usr/bin/say\n"
        with mock.patch.object(self.v.os, "kill") as kill:
            self.v.cmd_stop()
        kill.assert_called_once_with(4242, self.v.signal.SIGTERM)
        self.assertFalse(os.path.exists(self.v.SAY_PID))
        self.v.cmd_say("Ещё.")
        self.run.return_value.stdout = "vim\n"   # pid уже занят чужим процессом
        with mock.patch.object(self.v.os, "kill") as kill:
            self.v.cmd_stop()
        kill.assert_not_called()


class BotSendTest(unittest.TestCase):
    def test_file_name_cannot_break_header(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import bot_send
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=0):
            sent["body"] = req.data
            return Resp()
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh, \
                mock.patch.object(bot_send.urllib.request, "urlopen", fake), \
                mock.patch.object(bot_send.json, "load", return_value={"ok": True}):
            from pathlib import Path
            bot_send.call("t", "sendDocument", {"chat_id": "1"}, Path(fh.name), 'a"b\r\nX-Evil: 1.txt')
        self.assertIn(b'filename="abX-Evil: 1.txt"', sent["body"])


if __name__ == "__main__":
    unittest.main()
