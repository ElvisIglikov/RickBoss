#!/usr/bin/env python3
"""Живой голос Рика: локальный сервер страницы, где хозяин говорит с голосовой моделью OpenAI.

Слушает только 127.0.0.1. Ключ OpenAI живёт только здесь (voice/.env, OPENAI_API_KEY) и не уходит ни на страницу,
ни в журнал:
- GPT-Live-1 (основной): страница шлёт сюда своё SDP-предложение, сервер сам создаёт сессию и отдаёт SDP-ответ;
- gpt-realtime-2.1 (запасной): сервер выдаёт странице только временный ключ сессии (ek_…).
Просьбы голоса кладутся в почту Рика (tools/rick_bus.py tell_voice): ответ странице сразу, Рика будит ожидатель
(wake_on_mail=true), иначе фраза ждёт следующего слова хозяина. Ответы Рика сервер берёт из voice/otvety/spisok.txt
(туда их пишет rick_voice.py say) и потоком (SSE) отдаёт странице.

Сводка «что сейчас»: memory/NOW.md + строки журнала за сегодня с «решение/готово/ждёт»; кладётся в инструкции
сессии OpenAI (уходит в OpenAI!), на вопросы о делах голос отвечает сам, Рику отдаёт только поручения и решения
хозяина. /api/now — свежая сводка, страница спрашивает раз в минуту и досылает смену не чаще раза в 3 минуты.

Переменные: LIVE_PORT (8787), LIVE_VOICE (marin), RICK_HOME (корень Рика), LIVE_LOG_DIR (журнал сервера),
LIVE_BRIDGE=file — просьбы только в log/asks.txt, без почты Рика (проба), LIVE_ANSWERS — какой файл ответов слушать.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RICK = os.path.dirname(HERE)
ENV_FILE = os.path.join(RICK, "voice", ".env")
ANSWERS = os.environ.get("LIVE_ANSWERS") or os.path.join(RICK, "voice", "otvety", "spisok.txt")
sys.path.insert(0, os.path.join(RICK, "tools"))
import rick_bus  # noqa: E402  почта Рика: RICK_HOME, state/rick.json

BRIDGE = os.environ.get("LIVE_BRIDGE", "bus")
PORT = int(os.environ.get("LIVE_PORT", "8787"))
VOICE = os.environ.get("LIVE_VOICE", "marin")
LOG_DIR = os.environ.get("LIVE_LOG_DIR") or os.path.join(HERE, "log")
NOW_FILE = os.path.join(rick_bus.HOME, "memory", "NOW.md")
NOW_MAX = 12000  # символов сводки в инструкциях: больше голосу не нужно
NOW_WORDS = re.compile(r"(?i)решени|готово|ждёт|ждет")  # строки журнала, которые попадают в сводку голосу
MODELS = {"live": "gpt-live-1", "realtime": "gpt-realtime-2.1"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# Ключ страницы: /api/ask принимает фразу только от страницы, которую отдал этот процесс, —
# голос хозяина нельзя вбросить curl-ом из терминала или агента. Ключ живёт в памяти, на диск не пишется.
PAGE_TOKEN = secrets.token_hex(16)

PROMPT = """# Роль
Ты — «прихожая» Рика: голос, через который {owner} говорит со своим помощником Риком без клавиатуры.
Рик — умный помощник над всеми проектами хозяина. Он работает в отдельном окне и отвечает не сразу, через 10–60 секунд.

# Язык и тон
Говори только по-русски. Коротко: одна-две фразы. Спокойно, неторопливо, на «ты», без канцелярита и без лишней бодрости.
Если хозяин перебивает — сразу замолчи и слушай.

# Что отвечаешь сам
Простое: приветствие, болтовня, «ты меня слышишь», переспросить, повторить сказанное, уточнить непонятную просьбу.
Вопросы о делах — «что там», «как дела», «что по проекту», «что ждёт меня», «что делают терминалы»: отвечай сам по
сводке «что сейчас» в конце инструкций и её обновлениям — коротко, одна-три фразы: главное и что нужно от хозяина. Рика
ради этого не дёргай — он занят работой. В сводке ответа нет — так и скажи и предложи спросить Рика; отдашь, если хозяин
согласится.
Обновления сводки приходят молча со словами «Сводка обновлена» — вслух их не зачитывай, просто учитывай.

# Что отдаёшь Рику
Только поручения и решения хозяина: сделать, открыть, проверить, поручить, написать, отправить, новые задачи, его
«да», «выкладывай…», «отправляй», «спроси Рика». Сам их не выполняй и не выдумывай результат. {how}
Отдав, сразу скажи: «Передал Рику, отвечу, как он ответит» — и продолжай разговор, не молчи в ожидании.
Если мост скажет, что Рик занят, — скажи: «Рик занят — фраза у него в почте, ответит после текущего дела». Не говори, что не прошло.

# Ответ Рика
Ответ Рика приходит тебе текстом, он начинается словами «Рик ответил». Перескажи его хозяину своим голосом почти дословно,
ничего не добавляя от себя. Длинный — сократи до сути, но имена, числа и то, что нужно от хозяина, сохрани."""

HOW = {
    "live": "Отдавай делегированием: слова хозяина Рик получит как есть.",
    "realtime": "Отдавай инструментом ask_rick: в request положи просьбу хозяина полностью, его словами.",
}

ASK_RICK = {
    "type": "function",
    "name": "ask_rick",
    "description": "Передать Рику просьбу о работе или вопрос о проектах, терминалах, задачах и агентах хозяина. "
                   "Рик ответит позже, его ответ придёт сообщением «Рик ответил».",
    "parameters": {
        "type": "object",
        "properties": {"request": {"type": "string", "description": "Просьба хозяина полностью, его словами"}},
        "required": ["request"],
    },
}


# ---- журнал ----------------------------------------------------------------------------------------------

LOG_LOCK = threading.Lock()


def log(src, kind, **data):
    """Каждое событие с временем — в log/ГГГГ-ММ-ДД.jsonl (для замеров опыта)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    now = datetime.datetime.now()
    row = {"t": now.isoformat(timespec="milliseconds"), "src": src, "kind": kind, **data}
    with LOG_LOCK, open(os.path.join(LOG_DIR, now.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def scrub(text):
    """Ошибки OpenAI иногда цитируют хвост ключа — вырезаем любые sk-/ek- строки."""
    return re.sub(r"\b(sk|ek)-[\w*.-]+", r"\1-…", str(text))


# ---- OpenAI ----------------------------------------------------------------------------------------------

def load_key():
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("OPENAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def openai_post(path, body):
    key = load_key()
    if not key:
        return 500, {"error": "Нет ключа: впиши OPENAI_API_KEY в voice/.env"}
    req = urllib.request.Request(
        "https://api.openai.com/v1" + path, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error") or {}
            msg, code = err.get("message") or raw, err.get("code") or err.get("type") or ""
        except (ValueError, AttributeError):
            msg, code = raw, ""
        return e.code, {"error": scrub(f"OpenAI {e.code} {code}: {msg}")[:600]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 502, {"error": scrub(f"Нет связи с OpenAI: {e}")}


def now_summary():
    """Сводка «что сейчас» для голоса: memory/NOW.md и строки журнала за сегодня с «решение/готово/ждёт».
    v меняется только от них."""
    text, src = "", ""
    try:
        with open(NOW_FILE, encoding="utf-8") as f:
            text, src = f.read(), "NOW.md"
    except OSError:
        pass
    text = text.strip()
    if len(text) > NOW_MAX:  # режем по целой строке, свежее — в конце
        text = text[-NOW_MAX:].split("\n", 1)[-1]
    day = datetime.date.today().isoformat()
    try:
        with open(os.path.join(rick_bus.HOME, "journal", day + ".md"), encoding="utf-8") as f:
            events = [ln.rstrip("\n") for ln in f if ln.startswith("- ") and NOW_WORDS.search(ln)]
    except OSError:
        events = []
    room = NOW_MAX - len(text) - 60
    picked = []
    for ln in reversed(events):  # свежие строки журнала — в пределах NOW_MAX
        if len(ln) + 1 > room:
            break
        picked.insert(0, ln)
        room -= len(ln) + 1
    if picked:
        text += "\n\n## Журнал " + day + " (решения, готово, ждёт)\n" + "\n".join(picked)
        src = (src + " + journal") if src else "journal"
    return {"v": hashlib.sha1(text.encode()).hexdigest()[:12], "src": src, "text": text}


def instructions(engine, now):
    body = now["text"] or "Сводки нет: о делах скажи, что не знаешь, и предложи спросить Рика."
    return PROMPT.format(how=HOW[engine], owner=rick_bus.owner()) + "\n\n# Сводка «что сейчас» (" + (now["src"] or "нет") + ")\n" + body


def start_live(sdp):
    """GPT-Live-1: сессия создаётся на сервере, страница получает только SDP-ответ."""
    now = now_summary()
    session = {"model": MODELS["live"], "instructions": instructions("live", now),
               "audio": {"output": {"voice": VOICE}}, "delegation": {"type": "client"}}
    t0 = time.time()
    status, data = openai_post("/live/sessions", {"session": session, "transport": {"type": "webrtc", "sdp": sdp}})
    ms = int((time.time() - t0) * 1000)
    if status >= 300:
        log("server", "live.error", status=status, error=data.get("error"), ms=ms)
        return status, data
    sid = (data.get("session") or {}).get("id", "")
    log("server", "live.created", model=MODELS["live"], session=sid, ms=ms, now=now["src"], now_chars=len(now["text"]))
    return 200, {"id": sid, "sdp": (data.get("transport") or {}).get("sdp", ""), "model": MODELS["live"], "now": now}


def realtime_token():
    """gpt-realtime-2.1: временный ключ сессии для страницы, настоящий ключ остаётся здесь."""
    now = now_summary()
    session = {
        "type": "realtime", "model": MODELS["realtime"], "instructions": instructions("realtime", now),
        "audio": {"input": {"transcription": {"model": "gpt-4o-mini-transcribe", "language": "ru"},
                            "noise_reduction": {"type": "far_field"}},
                  "output": {"voice": VOICE}},
        "tools": [ASK_RICK], "tool_choice": "auto",
    }
    status, data = openai_post("/realtime/client_secrets", {"session": session})
    if status >= 300:
        log("server", "realtime.error", status=status, error=data.get("error"))
        return status, data
    log("server", "realtime.token", model=MODELS["realtime"])
    return 200, {"value": data.get("value", ""), "model": MODELS["realtime"], "now": now}


# ---- мост к Рику -----------------------------------------------------------------------------------------

ASK_LOCK = threading.Lock()
LAST_ASK = {"text": "", "t": 0.0}


def ask_rick(text, engine, delegation_id):
    """Фраза голоса — в почту Рика (rick_bus), ответ странице сразу: ok, queued (Рик занят), id."""
    text = re.sub(r"\s+", " ", text).strip()[:4000]
    if not text:
        return {"ok": False, "error": "пустая просьба"}
    with ASK_LOCK:  # двойной отправки Рику быть не должно
        if text == LAST_ASK["text"] and time.time() - LAST_ASK["t"] < 30:
            return {"ok": True, "how": "повтор, второй раз не отправлял"}
        LAST_ASK.update(text=text, t=time.time())
    msg = "Голос: " + text
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "asks.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\t{BRIDGE}\t{msg}\n")
    t0 = time.time()
    if BRIDGE == "file":
        result = {"ok": True, "how": "file"}
    else:
        try:
            got = rick_bus.tell_voice(text)
            # queued: Рик занят или его некому будить (wake_on_mail выключен) — фраза ждёт в почте
            result = {"ok": True, "queued": got["busy"] or not got["waiter"], "id": got["id"], "how": "bus",
                      "since": got.get("since", "")}
        except Exception as e:  # почта недоступна — честно сказать странице
            result = {"ok": False, "how": "bus", "error": f"почта Рика не приняла: {e}"}
    result["ms"] = int((time.time() - t0) * 1000)
    log("server", "ask", engine=engine, delegation=delegation_id, text=text, **result)
    return result


# ---- ответы Рика -----------------------------------------------------------------------------------------

ANS_COND = threading.Condition()
ANSWERS_LIST = []  # (время, номер строки в spisok, текст)


def watch_answers():
    """Следим за spisok.txt: строка «NNNN<tab>метка<tab>текст», её пишет rick_voice.py say после озвучки."""
    offset = os.path.getsize(ANSWERS) if os.path.exists(ANSWERS) else 0
    tail = b""
    while True:
        time.sleep(0.4)
        try:
            size = os.path.getsize(ANSWERS)
        except OSError:
            continue
        if size < offset:  # файл заменили — читаем заново с начала
            offset, tail = 0, b""
        if size == offset:
            continue
        with open(ANSWERS, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
        offset = size
        tail += chunk
        *lines, tail = tail.split(b"\n")
        for raw in lines:
            parts = raw.decode("utf-8", "replace").split("\t", 2)
            text = parts[-1].strip()
            if not text:
                continue
            num = parts[0] if len(parts) == 3 else ""
            with ANS_COND:
                ANSWERS_LIST.append((time.time(), num, text))
                ANS_COND.notify_all()
            log("server", "answer", num=num, text=text)


# ---- HTTP ------------------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RickLive/1"

    def log_message(self, *_):  # тише в консоли: всё нужное — в журнале
        pass

    def trusted(self):
        """Только наша страница: чужой сайт в браузере не должен слать просьбы Рику."""
        if self.headers.get("Host", "") not in HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.replace("http://", "") in HOSTS

    def reply(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.trusted():
            return self.reply(403, {"error": "только с localhost"})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read().replace(b"__LIVE_TOKEN__", PAGE_TOKEN.encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            # server — путь этого server.py: start.sh отличает свой сервер от сервера другой копии Рика на порту
            self.reply(200, {"ok": True, "server": os.path.realpath(__file__), "bridge": BRIDGE, "models": MODELS, "voice": VOICE,
                             "key": bool(load_key()), "rick": bool(rick_bus.rick().get("cli")),
                             "waiter": rick_bus.waiter_alive()})
        elif path == "/api/answers":
            self.stream_answers()
        elif path == "/api/now":
            self.reply(200, now_summary())
        else:
            self.reply(404, {"error": "нет такого"})

    def stream_answers(self):
        """SSE: новые ответы Рика. id события — номер в списке, EventSource сам догонит пропущенное."""
        last = self.headers.get("Last-Event-ID", "")
        with ANS_COND:
            pos = int(last) + 1 if last.isdigit() else len(ANSWERS_LIST)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                with ANS_COND:
                    if pos >= len(ANSWERS_LIST):
                        ANS_COND.wait(15)
                    fresh = ANSWERS_LIST[pos:]
                for i, (t, num, text) in enumerate(fresh, start=pos):
                    data = json.dumps({"num": num, "text": text, "t": t}, ensure_ascii=False)
                    self.wfile.write(f"id: {i}\ndata: {data}\n\n".encode())
                pos += len(fresh)
                if not fresh:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        if not self.trusted() or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.reply(403, {"error": "только с нашей страницы"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n <= 2_000_000 else {}
        except ValueError:
            return self.reply(400, {"error": "плохой JSON"})
        path = self.path.split("?", 1)[0]
        if path == "/api/session":
            self.reply(*start_live(str(body.get("sdp") or "")))
        elif path == "/api/token":
            self.reply(*realtime_token())
        elif path == "/api/ask":
            origin = (self.headers.get("Origin") or "").replace("http://", "")
            if origin not in HOSTS or not secrets.compare_digest(self.headers.get("X-Live-Token") or "", PAGE_TOKEN):
                log("server", "ask.rejected", origin=origin or "нет")
                return self.reply(403, {"error": "только со страницы live — обнови страницу"})
            self.reply(200, ask_rick(str(body.get("text") or ""), body.get("engine"), body.get("delegation_id")))
        elif path == "/api/log":
            for ev in (body.get("events") or [])[:50]:
                if isinstance(ev, dict):
                    log("page", str(ev.get("kind", "event")),
                        **{k: v for k, v in ev.items() if k not in ("t", "src", "kind")})
            self.reply(200, {"ok": True})
        else:
            self.reply(404, {"error": "нет такого"})


def main():
    threading.Thread(target=watch_answers, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True
    log("server", "start", port=PORT, bridge=BRIDGE, answers=ANSWERS)
    print(f"Живой голос Рика: http://127.0.0.1:{PORT}/  (мост: {BRIDGE})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
