"""Тест моста голоса через почту Рика (live/server.py + tools/rick_bus.py).

Сервер поднимается на тестовом порту (LIVE_PORT, по умолчанию 8799) с песочными RICK_HOME, LIVE_LOG_DIR и
LIVE_ANSWERS. Живой сервер 8787 и настоящие journal/, inbox/, state/, live/log/ не трогаются. Сервер гасится по pid.
OpenAI не вызывается.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

LIVE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RICK = os.path.dirname(LIVE)
sys.path.insert(0, os.path.join(RICK, "tools"))
import rick_bus as bus  # noqa: E402


def free_port(want):
    for port in [want] + list(range(8800, 8840)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("нет свободного порта")


class ServerBusTest(unittest.TestCase):
    bridge = "bus"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        cls.home = os.path.join(root, "home")
        for d in ("memory", "journal"):
            os.makedirs(os.path.join(cls.home, d))
        os.makedirs(os.path.join(root, "log"))
        with open(os.path.join(cls.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"owner": "Тестер"}, f, ensure_ascii=False)
        with open(os.path.join(cls.home, "memory", "NOW.md"), "w", encoding="utf-8") as f:
            f.write("# Сейчас\n- Сайт: ждёт «выкладывай форму»\n")
        cls.day = os.path.join(cls.home, "journal", time.strftime("%Y-%m-%d") + ".md")
        with open(cls.day, "w", encoding="utf-8") as f:
            f.write("# Журнал\n- 00:01 · [агент] Сайт · форма готово · Рик\n- 00:02 · [агент] Рик · открыл терминал · —\n")
        cls.port = free_port(int(os.environ.get("LIVE_PORT", "8799")))
        cls.env = dict(os.environ, LIVE_PORT=str(cls.port), RICK_HOME=cls.home, LIVE_LOG_DIR=os.path.join(root, "log"),
                       LIVE_ANSWERS=os.path.join(root, "spisok.txt"), LIVE_BRIDGE=cls.bridge)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(LIVE, "server.py")], env=cls.env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.get("/api/health")
                break
            except OSError:
                time.sleep(0.05)
        cls.saved = (bus.HOME, os.environ.get("RICK_HOME"))
        bus.HOME = cls.home
        os.environ["RICK_HOME"] = cls.home

    @classmethod
    def tearDownClass(cls):
        os.kill(cls.proc.pid, signal.SIGTERM)  # по pid, не pkill
        cls.proc.wait(5)
        bus.HOME, old = cls.saved
        if old is None:
            os.environ.pop("RICK_HOME", None)
        else:
            os.environ["RICK_HOME"] = old
        cls.tmp.cleanup()

    @classmethod
    def get(cls, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path),
                                     headers={"Host": "127.0.0.1:%d" % cls.port})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    @classmethod
    def page(cls):
        req = urllib.request.Request("http://127.0.0.1:%d/" % cls.port, headers={"Host": "127.0.0.1:%d" % cls.port})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode("utf-8")

    @classmethod
    def page_token(cls):
        """Ключ страницы — из отданной сервером index.html, как у браузера."""
        return re.search(r'const LIVE_TOKEN = "([0-9a-f]{32})"', cls.page()).group(1)

    def ask(self, text, token=None, origin=True):
        headers = {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json",
                   "X-Live-Token": self.page_token() if token is None else token}
        if origin:
            headers["Origin"] = "http://127.0.0.1:%d" % self.port
        req = urllib.request.Request("http://127.0.0.1:%d/api/ask" % self.port, method="POST",
                                     data=json.dumps({"text": text, "engine": "live"}).encode(), headers=headers)
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r), time.time() - t0

    def setUp(self):
        for name in ("waiter.json", "turn.json"):
            try:
                os.remove(os.path.join(self.home, "state", name))
            except OSError:
                pass


class BusBridge(ServerBusTest):
    def test_0_forged_voice_rejected(self):
        """curl без ключа страницы или без Origin — 403, в почту ничего не легло."""
        for kw in ({"token": ""}, {"token": "0" * 32}, {"origin": False}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.ask("да, отправляй рассылку", **kw)
            self.assertEqual(cm.exception.code, 403)
        self.assertFalse([i for i in bus._read_jsonl(os.path.join(self.home, "inbox", "queue.jsonl"))
                          if "рассылку" in (i.get("text") or "")])

    def test_1_ask_fast_queued_without_waiter(self):
        res, dt = self.ask("открой сайт")
        self.assertLess(dt, 0.3, "ответ странице %.3f с" % dt)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["queued"])  # будить некому — фраза ждёт в почте
        self.assertEqual(res["how"], "bus")
        self.assertTrue(res["id"])
        self.assertEqual([i["text"] for i in bus.pending(("voice",))], ["открой сайт"])
        with open(self.day, encoding="utf-8") as f:
            self.assertIn("[чел] Тестер (голос) → [агент] Рик · открой сайт", f.read())
        with open(os.path.join(self.env["LIVE_LOG_DIR"], "asks.txt"), encoding="utf-8") as f:
            self.assertIn("Голос: открой сайт", f.read())

    def test_2_duplicate_within_30s(self):
        self.ask("закрой терминал блога")
        res, _ = self.ask("закрой терминал блога")
        self.assertIn("повтор", res["how"])
        self.assertEqual(sum(1 for i in bus.pending(("voice",)) if i["text"] == "закрой терминал блога"), 1)

    def test_3_live_waiter_free_rick_not_queued(self):
        bus._write_json(os.path.join(self.home, "state", "waiter.json"), {"pid": os.getpid()})
        res, dt = self.ask("что там по блогу")
        self.assertLess(dt, 0.3)
        self.assertTrue(res["ok"])
        self.assertFalse(res["queued"])
        bus._write_json(os.path.join(self.home, "state", "turn.json"),
                        {"busy": True, "since": time.time() - 40, "last": time.time()})
        res, _ = self.ask("и по сайту")
        self.assertTrue(res["queued"])
        health = self.get("/api/health")
        self.assertEqual(health["bridge"], "bus")
        self.assertEqual(health["server"], os.path.realpath(os.path.join(LIVE, "server.py")))   # для start.sh
        self.assertTrue(health["waiter"])
        self.assertFalse(health["key"])  # voice/.env в песочнице нет — ключа нет

    def test_4_now_summary(self):
        a = self.get("/api/now")
        self.assertIn("выкладывай форму", a["text"])
        self.assertIn("форма готово", a["text"])
        self.assertNotIn("открыл терминал", a["text"])
        self.assertEqual(self.get("/api/now")["v"], a["v"])
        with open(self.day, "a", encoding="utf-8") as f:
            f.write("- 00:03 · [агент] Рик · прочитал память · —\n")
        self.assertEqual(self.get("/api/now")["v"], a["v"])  # обычная строка журнала v не меняет
        with open(self.day, "a", encoding="utf-8") as f:
            f.write("- 00:04 · [агент] Блог → [агент] Рик · ждёт хозяина: тема поста · tell\n")
        b = self.get("/api/now")
        self.assertNotEqual(b["v"], a["v"])
        self.assertIn("тема поста", b["text"])

    def test_5_page_served_and_compatible(self):
        page = self.page()
        self.assertIn("Рик занят — фраза у него в почте, ответит после текущего дела", page)
        self.assertIn("NOW_MIN_GAP_MS", page)
        self.assertIn("r.queued", page)

    def test_6_instructions_use_owner(self):
        sys.path.insert(0, LIVE)
        import server  # noqa: E402  импорт без запуска: RICK_HOME песочницы
        text = server.instructions("live", server.now_summary())
        self.assertIn("через который Тестер говорит", text)
        self.assertIn("выкладывай форму", text)


class FileBridge(ServerBusTest):
    bridge = "file"

    def test_file_bridge_as_before(self):
        res, dt = self.ask("проба без Рика")
        self.assertEqual(res["how"], "file")
        self.assertEqual(bus.pending(), [])
        with open(os.path.join(self.env["LIVE_LOG_DIR"], "asks.txt"), encoding="utf-8") as f:
            self.assertIn("\tfile\tГолос: проба без Рика", f.read())


if __name__ == "__main__":
    unittest.main()
