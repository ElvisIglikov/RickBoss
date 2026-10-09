"""Тесты tools/statusline.py: запуск как у Claude Code — JSON на stdin, строка на stdout; память и tmux подменные."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(TOOLS, "statusline.py")
sys.path.insert(0, TOOLS)
import statusline  # noqa: E402

# Подменный tmux — shell, а не python: у настоящего на ответ 0,3 с, холодный python столько и стартует
FAKE_TMUX = "#!/bin/sh\nprintf 'rick-Site\\nrick-Blog\\nrick-Shop\\nother\\n'\n"


def usage(fresh, read, made):
    return {"input_tokens": fresh, "cache_read_input_tokens": read, "cache_creation_input_tokens": made,
            "output_tokens": 50}


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.tmux = os.path.join(self.home, "tmux")
        with open(self.tmux, "w") as fh:
            fh.write(FAKE_TMUX)
        os.chmod(self.tmux, 0o755)
        subprocess.run([self.tmux], capture_output=True, timeout=10)   # первый запуск нового файла macOS проверяет дольше 0,3 с

    def put(self, rel, text):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def run_line(self, stdin, tmux=None):
        env = dict(os.environ, RICK_HOME=self.home, RICK_TMUX=tmux or os.path.join(self.home, "нет-tmux"))
        done = subprocess.run([sys.executable, SCRIPT], input=stdin, capture_output=True, text=True, env=env,
                              timeout=10)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "")
        return done.stdout.rstrip("\n")

    def test_full_line(self):
        self.put("inbox/queue.jsonl", "".join(json.dumps({"id": i, "text": "x"}) + "\n" for i in range(1, 5)))
        self.put("inbox/delivered.jsonl", '{"id": 1}\n{"id": 2}\n')
        self.put("memory/tasks.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in (
            {"id": 1, "title": "а", "status": "open"}, {"id": 2, "title": "б", "status": "open"},
            {"id": 2, "title": "б", "status": "done"}, {"id": 3, "title": "в", "status": "doing"},
            {"id": 4, "title": "г", "status": "cancelled"})) + "\nбитая строка\n")
        data = {"model": {"display_name": "Opus"}, "context_window": {"current_usage": usage(1000, 180000, 6000)}}
        self.assertEqual(self.run_line(json.dumps(data), self.tmux),
                         "🔰 Рик · 187K · кэш 96% · 3 чата · 📥 2 · 2 задачи")

    def test_transcript_fallback(self):
        rows = [{"type": "assistant", "message": {"usage": usage(10, 50000, 40000)}},
                {"type": "assistant", "isSidechain": True, "message": {"usage": usage(1, 1, 1)}},
                {"type": "user", "message": {"content": "привет"}}]
        path = self.put("t.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n{битый\n")
        self.assertEqual(self.run_line(json.dumps({"transcript_path": path})), "🔰 Рик · 90K · кэш 56%")

    def test_empty_memory_and_no_tmux(self):
        self.assertEqual(self.run_line("{}"), "🔰 Рик")
        self.assertFalse(os.path.exists(os.path.join(self.home, "memory")))   # только читает
        self.assertEqual(self.run_line(json.dumps({"transcript_path": "/нет/такого.jsonl"})), "🔰 Рик")

    def test_bad_input_is_silent(self):
        self.assertEqual(self.run_line("не json"), "")
        self.assertEqual(self.run_line(""), "🔰 Рик")
        self.assertEqual(self.run_line("[]"), "🔰 Рик")

    def test_plural(self):
        self.assertEqual([statusline.plural(n, "чат", "чата", "чатов") for n in (1, 2, 5, 11, 12, 21, 22, 25)],
                         ["чат", "чата", "чатов", "чатов", "чатов", "чат", "чата", "чатов"])


if __name__ == "__main__":
    unittest.main()
