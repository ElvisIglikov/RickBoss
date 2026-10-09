"""Тесты tools/notify.py: osascript и afplay подменены скриптом, который пишет, что ему передали."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0")" "$@" >> "$FAKE_OUT"
[ "$(basename "$0")" = osascript ] && cat > "$FAKE_OUT.script"
[ -n "$FAKE_ERR" ] && { echo "$FAKE_ERR" >&2; exit 1; }
exit 0
"""
HAS_GLASS = os.path.exists("/System/Library/Sounds/Glass.aiff")


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "calls")
        for name in ("osascript", "afplay"):
            with open(os.path.join(self.tmp.name, name), "w") as f:
                f.write(FAKE)
            os.chmod(os.path.join(self.tmp.name, name), 0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, notify):
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            json.dump({"notify": notify}, f)

    def run_cli(self, *args, err=""):
        env = dict(os.environ, RICK_HOME=self.tmp.name, RICK_OSASCRIPT=os.path.join(self.tmp.name, "osascript"),
                   RICK_AFPLAY=os.path.join(self.tmp.name, "afplay"), FAKE_OUT=self.out, FAKE_ERR=err)
        return subprocess.run([PY, os.path.join(TOOLS, "notify.py"), *args], env=env, capture_output=True,
                              text=True, timeout=20)

    def calls(self):
        with open(self.out) as f:
            return f.read().splitlines()

    @unittest.skipUnless(HAS_GLASS, "нет системных звуков Мака")
    def test_banner_and_sound_by_default(self):
        r = self.run_cli('Готово: отчёт «срочно" собран', "--title", "Рик · Сайт")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["osascript", "-", 'Готово: отчёт «срочно" собран', "Рик · Сайт",
                                        "afplay", "/System/Library/Sounds/Glass.aiff"])
        with open(self.out + ".script") as f:
            self.assertIn("display notification (item 1 of argv)", f.read())   # текст не вклеен в код
        self.assertIn("баннер и звук Glass", r.stdout)

    def test_config_switches(self):
        self.config({"sound": False})
        r = self.run_cli("готово")
        self.assertEqual((r.returncode, self.calls()[0]), (0, "osascript"), r.stderr)
        self.assertNotIn("afplay", self.calls())
        os.remove(self.out)
        self.config({"sound": False, "banner": False})
        r = self.run_cli("готово")
        self.assertIn("выключены", r.stdout)
        self.assertFalse(os.path.exists(self.out))

    def test_errors_are_plain(self):
        self.config({"banner": False})
        for sound, want in (("../../etc/passwd", "словом"), ("NoSuchSound", "Звука NoSuchSound нет")):
            r = self.run_cli("готово", "--sound", sound)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(want, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        self.config({})
        r = self.run_cli("готово", "--sound", "", err="execution error: Not authorized (-1743)")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("osascript: execution error", r.stderr)
        r = self.run_cli("   ")
        self.assertIn("Пустое", r.stderr)


if __name__ == "__main__":
    unittest.main()
