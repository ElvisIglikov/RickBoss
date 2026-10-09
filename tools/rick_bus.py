#!/usr/bin/env python3
"""Почта Рика: голос хозяина и ответы терминальных чатов доходят до Рика без вставки в поле ввода.

Файлы (корень — rick_config.home(): $RICK_HOME или папка над tools/; каталоги код создаёт сам):
  inbox/queue.jsonl       входящие, только дописываются: {"id", "t", "at", "kind", "from", "text"}
  inbox/delivered.jsonl   id доставленного, только дописываются
  inbox/.lock             одна блокировка на все изменения почты (fcntl)
  inbox/archive/          доставленное старше суток; архив старше 7 дней удаляется (там бывают телефоны)
  state/rick.json         {cli, cwd, claimed_at[, local]}: какая сессия Claude Code — Рик
  state/turn.json         занят ли Рик и с какого времени, чьё слово в ходе (пишут хуки mem_hook.py)
  state/waiter.json       pid живого ожидателя
  journal/ГГГГ-ММ-ДД.md   хроника дня: одна запись — одна строка O_APPEND, местное время

Роль Рика: главная сессия (без agent_id), запущенная в корне Рика, заявляет себя хуком SessionStart
(claim_session). Запасной путь — `claim <local_…>` по индексу Claude Desktop, если он есть.

Виды почты: voice — только tell_voice() из live/server.py (командной строкой голос хозяина не подделать);
window — ответ терминала, `tell --from <Проект>`. Заметки Рика (`note`) — строки журнала, не почта.
Ожидатель (`wait`, хук с asyncRewake) будит Рика только при wake_on_mail=true в config.json; иначе почту
приносит хук prompt со следующим словом хозяина.

CLI: tell --from <Проект> "<строка>" | note "<строка>" | claim <local_…> | state | pending | wait (только из хука)
"""
import datetime
import fcntl
import glob
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402

HOME = rick_config.home()
CCD = os.environ.get("RICK_CCD_DIR") or os.path.expanduser(
    "~/Library/Application Support/Claude/claude-code-sessions")

BATCH_SEC = 20          # ответы терминалов — пачкой через 20 с после первой строки
WARM_SEC = 55 * 60      # будить ответами терминалов, только пока кеш Рика тёплый
BUSY_STALE = 30 * 60    # «занят» без событий дольше — считаем свободным (прерванный ход Stop не шлёт)
WAIT_MAX = 43000        # ожидатель сам уходит до тайм-аута хука 43 200 с
POLL = 0.5
START_GRACE = 5         # после SessionStart первое слово — хуку prompt (ранний exit 2 срывал первый запрос)
LOCK_WAIT = 1.0         # не взял блокировку за ~1 с — не доставлять сейчас
KEEP_SEC = 24 * 3600
ARCHIVE_DAYS = 7
TEXT_MAX = 4000

_now = time.time


class LockBusy(Exception):
    """Почту держит другой процесс дольше LOCK_WAIT."""


def owner():
    return str(rick_config.load().get("owner") or "хозяин")


def wake_on_mail():
    return bool(rick_config.load().get("wake_on_mail"))


# ---- файлы ------------------------------------------------------------------------------------------------

def _p(*parts):
    return os.path.join(HOME, *parts)


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else ({} if default is None else default)
    except (OSError, ValueError):
        return {} if default is None else default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # недописанная строка — пропустить
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _append_lines(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


class _Locked:
    """Одна блокировка inbox/.lock на все изменения почты."""

    def __init__(self, wait=LOCK_WAIT):
        self.wait = wait
        self.fd = None

    def __enter__(self):
        os.makedirs(_p("inbox"), exist_ok=True)
        self.fd = os.open(_p("inbox", ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise LockBusy("почта занята другим процессом")
                time.sleep(0.02)

    def __exit__(self, *_):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# ---- очистка и журнал -------------------------------------------------------------------------------------

_SECRET_WORD = re.compile(r"(?i)\b(пароль|ключ|token|токен|password)\b[^·\n]*")  # до конца поля «·»
_KEYS = re.compile(r"\b(?:sk|ek)[-_][\w*.-]{6,}|\bghp_\w{8,}|\bgithub_pat_\w{8,}|\bxox[abprs]-[\w-]{8,}"
                   r"|\b\d{8,10}:[\w-]{30,}")  # + ключи вида sk_… и токены ботов Telegram
_PHONE = re.compile(r"(?<![\w+])(?:\+7|8|7)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_B64 = re.compile(r"(?<![\w+/=-])[A-Za-z0-9+/=_-]{40,}")


def _b64(m):
    s = m.group(0)
    if s.count("/") >= 3 or not (re.search(r"[A-Z]", s) and re.search(r"[a-z]", s) and re.search(r"\d", s)):
        return s  # путь или id с дефисами — не секрет
    return "[скрыто]"


def clean(text):
    """Перед записью в журнал: телефоны, ключи и токены, длинные hex/base64, текст после «пароль/ключ/token»."""
    text = _SECRET_WORD.sub(lambda m: m.group(1) + " …", str(text))
    text = _KEYS.sub("[ключ]", text)
    text = _PHONE.sub("[телефон]", text)
    text = _HEX.sub("[hex]", text)
    return _B64.sub(_b64, text)


def one_line(text, limit=None):
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def journal(body, t=None):
    """Строка «- ЧЧ:ММ · <body>» в journal/<день>.md одной записью O_APPEND. Возвращает строку или ""."""
    body = clean(one_line(body, 700))
    if not body:
        return ""
    dt = datetime.datetime.fromtimestamp(t or _now())
    path = _p("journal", dt.strftime("%Y-%m-%d") + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "- %s · %s\n" % (dt.strftime("%H:%M"), body)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        head = ("# Журнал %s\nСтрока: время · кто → кому · суть · источник или по чьему слову. [чел] — человек, "
                "[агент] — нейросеть. Время — местное.\nТолько дописывается: строки пишут хуки mem_hook.py и "
                "tools/rick_bus.py (tell, note).\n\n" % dt.strftime("%d.%m.%Y"))
        line = head + line
    except FileExistsError:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND, 0o644)
        try:  # прежняя запись без перевода строки — не склеивать
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = "\n" + line
        except OSError:
            pass
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return line.rstrip("\n").split("\n")[-1]


def note(text):
    """Строка журнала от Рика. Своя разметка «[чел]/[агент]» — как есть, иначе «[агент] Рик · …»."""
    text = re.sub(r"^[-\s]*~?\d{1,2}:\d{2}\s*·\s*", "", one_line(text, 600))  # время ставит journal()
    if not text:
        raise ValueError("пустая заметка")
    return journal(text if re.search(r"\[(чел|агент)", text) else "[агент] Рик · " + text)


# ---- Рик: заявка роли и опознание -------------------------------------------------------------------------

def rick():
    return _read_json(_p("state", "rick.json"))


def _same_dir(a, b):
    return bool(a) and os.path.realpath(str(a)) == os.path.realpath(str(b))


def claim_session(inp):
    """Хук SessionStart: главная сессия Claude Code (без agent_id) в корне Рика — это Рик. → True, если заявила."""
    sid = str(inp.get("session_id") or "")
    if inp.get("agent_id") or not sid or not _same_dir(inp.get("cwd"), HOME):
        return False
    rec = rick()
    if rec.get("cli") == sid:
        return True
    rec = {"cli": sid, "cwd": HOME, "source": str(inp.get("source") or ""),
           "claimed_at": datetime.datetime.fromtimestamp(_now()).isoformat(timespec="seconds")}
    _write_json(_p("state", "rick.json"), rec)
    journal("[агент] Рик · роль Рика — сессия %s (%s) · SessionStart" % (sid[:8], rec["source"] or "старт"))
    return True


def _index(local):
    """Запасной путь: файл индекса Claude Desktop …/claude-code-sessions/*/*/<local_…>.json. Нет — {}."""
    for path in glob.glob(os.path.join(glob.escape(CCD), "*", "*", local + ".json")):
        data = _read_json(path)
        if data:
            return data
    return {}


def claim(local):
    """Запасной путь: роль Рика — чату Claude Desktop local_…. Чат должен быть в папке Рика и не в архиве."""
    local = str(local or "").strip()
    if not re.fullmatch(r"local_[0-9a-f-]{8,64}", local):
        raise ValueError("нужен id чата вида local_…")
    data = _index(local)
    if not data:
        raise LookupError("чата %s нет в индексе Claude Desktop" % local)
    cwd = data.get("cwd") or data.get("originCwd") or ""
    if not _same_dir(cwd, HOME):
        raise ValueError("чат %s не в папке Рика (%s)" % (local, cwd))
    if data.get("isArchived"):
        raise ValueError("чат %s в архиве" % local)
    rec = {"local": local, "cli": data.get("cliSessionId") or "", "cwd": HOME,
           "claimed_at": datetime.datetime.fromtimestamp(_now()).isoformat(timespec="seconds")}
    _write_json(_p("state", "rick.json"), rec)
    journal("[агент] Рик · роль Рика — чат %s (сессия %s) · claim" % (local, (rec["cli"] or "ещё нет")[:8]))
    return rec


def is_rick(session_id, agent_id=None):
    """Сессия хука — Рик? Субагент (agent_id) — нет. Не совпало и есть Desktop-заявка — сверить её индекс."""
    if agent_id or not session_id:
        return False
    rec = rick()
    if rec.get("cli") and rec.get("cli") == session_id:
        return True
    if not rec.get("local"):
        return False
    cli = _index(rec["local"]).get("cliSessionId") or ""
    if cli and cli == session_id:  # чат Рика сменил сессию или cli был пуст при claim
        rec["cli"] = cli
        _write_json(_p("state", "rick.json"), rec)
        return True
    return False


# ---- состояние хода ---------------------------------------------------------------------------------------

def turn():
    return _read_json(_p("state", "turn.json"))


def save_turn(data):
    _write_json(_p("state", "turn.json"), data)


def busy_since():
    """Время начала хода, если Рик занят; 0 — свободен."""
    t = turn()
    if t.get("busy") and _now() - float(t.get("last") or t.get("since") or 0) < BUSY_STALE:
        return float(t.get("since") or 0)
    return 0


def waiter_alive():
    pid = _read_json(_p("state", "waiter.json")).get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def voice_taken():
    """Когда голос последний раз доставлен Рику."""
    return float(_read_json(_p("state", "voice_taken.json")).get("t") or 0)


def mark_voice_taken(ids=()):
    _write_json(_p("state", "voice_taken.json"), {"t": _now(), "ids": list(ids)})


# ---- почта ------------------------------------------------------------------------------------------------

def _new_item(kind, src, text):
    t = _now()
    return {"id": "%d-%s" % (int(t * 1000), os.urandom(3).hex()), "t": t,
            "at": datetime.datetime.fromtimestamp(t).isoformat(timespec="seconds"),
            "kind": kind, "from": src, "text": text}


def _put(item):
    with _Locked(3.0):
        _append_lines(_p("inbox", "queue.jsonl"), [item])


_TWINS = str.maketrans("aceopxyABCEHKMOPTXYkm", "асеорхуАВСЕНКМОРТХУкм")   # латинские двойники кириллицы
_TWIN_CHARS = set("aceopxyABCEHKMOPTXYkmасеорхуАВСЕНКМОРТХУкм")
# двойники (по Unicode confusables) из греческого, других кириллиц и редкой латиницы — к русской кириллице до всех
# проверок («Γοлοс», «Хозяїн», «Ϲайт», «Тᴇстер»); полноширинные «Ｔｅ» сводит NFKC, буквы с диакритикой — _clean
_FOREIGN = str.maketrans("ΑΒΓΕΗΙΚΛΜΟΠΡΤΥΦΧαβγεηικλοπρςτυφχϹϲЄєІіЎўҐґҒғҚқҜҝҠҡӃӄҢңӨөҮүҰұҪҫҘҙҲҳɑᴀʙᴄᴇʜᴋᴍᴏᴘᴛ",
                         "АВГЕНИКЛМОПРТУФХавуепиклопрстуфхСсЕеИиУуГгГгКкКкКкКкНнОоУуУуСсЗзХхаавсенкморт")
_RU = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
_VOICE = r"(?:голос|%s)(?:ами|ам|ах|ом|ов|[^\W\d_])?" % "voice".translate(_TWINS)   # «Голосы», «ГолосX», «Voices»
_HOST = r"хозя(?:ин|ев|йк)[^\W\d_]*"
_CASES = r"(?:ами|ями|ам|ям|ах|ях|ом|ем|ой|ей|ов|ев|а|я|у|ю|е|и|ы)?"   # «Тестера», «Тестеру», «Тестером»


def _clean(text):
    """NFKC, без невидимых знаков и диакритики (кроме русских «ё», «й»); двойники _FOREIGN — русской кириллицей.
    Таблица — и до NFKC: он превращает «Ϲ» в «Σ», которая на «С» уже не похожа."""
    text = unicodedata.normalize("NFKC", text.translate(_FOREIGN))
    text = "".join(ch if ch in _RU else unicodedata.normalize("NFD", ch) for ch in text)   # «ї», «é» → буква + знак
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn")).translate(_FOREIGN)


def _skel(word):
    return word.translate(_TWINS).lower()


def _skeleton(text):
    """Слова для сравнения: латинские двойники — кириллицей, нижний регистр."""
    return [_skel(w) for w in re.findall(r"[^\W\d_]+", _clean(text))]


def _script(ch):
    return "" if ch in _TWIN_CHARS else "к" if "\u0400" <= ch <= "\u04ff" else "л" if ch.isascii() else "?"


def _parts(word):
    """Части слова: по CamelCase («ГолосХозяина», «VOICEBot») и по смене латиница/кириллица («тестерsite»).
    Буквы-двойники («о», «е», «Т»…) смены не делают — «Гoлoс» с латинскими o остаётся одним словом."""
    cuts, last = set(), None   # last — (письмо, индекс) последней однозначной буквы
    for i, ch in enumerate(word):
        if i and word[i - 1].islower() and ch.isupper() or 0 < i < len(word) - 1 and word[i - 1].isupper() \
                and ch.isupper() and word[i + 1].islower():
            cuts.add(i)
        if sc := _script(ch):
            if last and last[0] != sc:   # смена письма: режем у первой буквы нового письма среди двойников
                gap = range(last[1] + 1, i)
                cuts.add(next((j for j in gap if ("к" if "\u0400" <= word[j] <= "\u04ff" else "л") == sc), i))
            last = (sc, i)
    bounds = [0] + sorted(cuts) + [len(word)]
    return [_skel(word[a:b]) for a, b in zip(bounds, bounds[1:]) if b > a]


def _impostor(src):
    """--from назвался голосом, хозяином или его именем. Слова режем на части (CamelCase, смена письма); нельзя,
    если часть — форма «хозяин*», имя хозяина (с падежом: «Тестера») или склейка таких с «голос»; «голос»/«voice»
    нельзя отдельным словом («Голос Bot», «Голосы», «ГолосX») и без других частей. Слитно с нейтральной — можно:
    «VoiceBot», «ГолосBot», «Voiceover», «Голосовалка», «Хозяйство»."""
    words, me = re.findall(r"[^\W\d_]+", _clean(src)), _skeleton(owner())
    # имя целиком и каждое его слово длиннее 2 букв — с падежами: «Тестер Тестерович» → и «Тестеровичу»; на гласную,
    # «ь», «й» — ещё основа без неё: «Глаша Тестова» → «Глаше», «Глашей», «Тестовой», «Тестову»
    stems = {w[:-1] for w in me if len(w) > 3 and w[-1] in "аяоеьй"}
    name = [re.escape(n) + _CASES for n in sorted({"".join(me)} | {w for w in me if len(w) > 2} | stems) if n]
    hard = re.compile("(?:%s)*(?:%s)(?:%s)*" % ("|".join([_VOICE, _HOST] + name), "|".join([_HOST] + name),
                                                 "|".join([_VOICE, _HOST] + name)))
    for view in ([_skel(w) for w in words], [p for w in words for p in _parts(w)]):   # слово целиком и по частям
        if any(hard.fullmatch(p) or re.match(_HOST, p) for p in view) or me and any(
                view[i:i + len(me)] == me for i in range(len(view))):
            return True
        real = [p for p in view if len(p) > 1]   # одна буква — шум («ГолосX»)
        if real and all(re.fullmatch(_VOICE, p) for p in real):
            return True
    if any(re.fullmatch(_VOICE, _skel(w)) for w in words):   # «Голос» отдельным словом: «Голос Bot», «voice_1»
        return True
    return False


def tell(src, text):
    """Ответ терминала Рику. Имя проекта обязательно; назваться хозяином или голосом нельзя."""
    src, text = one_line(re.sub(r"[:«»\"📥]", " ", str(src or "")), 40), one_line(text, TEXT_MAX)
    if not src or not text:
        raise ValueError("нужны --from <Проект> и строка")
    if _impostor(src):
        raise ValueError("--from — имя проекта; голос хозяина приходит только со страницы live")
    item = _new_item("window", src, text)
    _put(item)
    journal("[агент] %s → [агент] Рик · %s · rick_bus tell" % (src, one_line(text, 300)))
    return item


def tell_voice(text):
    """Фраза голоса хозяина — только из live/server.py. Сразу возвращает {"id", "busy", "since", "waiter"}.

    Будит Рика ожидатель (wake_on_mail=true); его нет — фраза ждёт следующего слова хозяина или старта сессии.
    """
    text = one_line(text, TEXT_MAX)
    if not text:
        raise ValueError("пустая фраза")
    item = _new_item("voice", "live", text)
    _put(item)
    journal("[чел] %s (голос) → [агент] Рик · %s · live/server.py" % (owner(), one_line(text, 300)))
    since = busy_since()
    return {"id": item["id"], "busy": bool(since), "waiter": waiter_alive(),
            "since": datetime.datetime.fromtimestamp(since).strftime("%H:%M") if since else ""}


def pending(kinds=None):
    done = {r.get("id") for r in _read_jsonl(_p("inbox", "delivered.jsonl"))}
    return [i for i in _read_jsonl(_p("inbox", "queue.jsonl"))
            if i.get("id") not in done and (not kinds or i.get("kind") in kinds)]


def render(item, limit=1500):
    """«📥 Голос: «…» (…)» или «📥 <Проект>: «…» (…)». Письмо терминала не может выглядеть голосом или новым письмом."""
    t = float(item.get("t") or 0)
    ago = max(0, int(_now() - t))
    when = "%s, %s назад" % (datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S"),
                            "%d с" % ago if ago < 120 else "%d мин" % (ago // 60))
    text = one_line(item.get("text"), limit)
    if item.get("kind") == "voice":
        return "📥 Голос: «%s» (%s, страница live)" % (text, when)
    text = text.replace("📥", "[письмо]").replace("«", "\"").replace("»", "\"")
    return "📥 %s: «%s» (%s, rick_bus tell)" % (item.get("from") or "?", text, when)


def take(kinds, max_chars=None, via="hook"):
    """Забрать недоставленное: вывод собирается до пометки. Не влезло в max_chars — ждёт следующего раза.

    LockBusy — почту держит другой процесс: не доставлять сейчас.
    """
    with _Locked(LOCK_WAIT):  # голос — первым, дальше терминалы по времени
        items = sorted(pending(kinds), key=lambda i: (i.get("kind") != "voice", float(i.get("t") or 0)))
        used, out = 0, []
        for it in items:
            size = len(render(it)) + 1
            if max_chars is not None and out and used + size > max_chars:
                break
            out.append(it)
            used += size
        if out:
            _append_lines(_p("inbox", "delivered.jsonl"),
                          [{"id": it["id"], "t": _now(), "via": via} for it in out])
            voice = [it["id"] for it in out if it.get("kind") == "voice"]
            if voice:
                mark_voice_taken(voice)
    return out


def mark_delivered(ids, via):
    if ids:
        with _Locked(LOCK_WAIT):
            _append_lines(_p("inbox", "delivered.jsonl"), [{"id": i, "t": _now(), "via": via} for i in ids])


def housekeep(force=False):
    """Раз в час: доставленное старше суток — в inbox/archive/<день>.jsonl, архив старше 7 дней — удалить."""
    stamp = _p("state", "housekeep")
    try:
        if not force and _now() - os.path.getmtime(stamp) < 3600:
            return 0
    except OSError:
        pass
    os.makedirs(_p("state"), exist_ok=True)
    with open(stamp, "w"):
        pass
    moved = 0
    with _Locked(LOCK_WAIT):
        queue = _read_jsonl(_p("inbox", "queue.jsonl"))
        done = {r.get("id"): r for r in _read_jsonl(_p("inbox", "delivered.jsonl"))}
        old = [i for i in queue if i.get("id") in done and _now() - float(i.get("t") or 0) > KEEP_SEC]
        if old:
            by_day = {}
            for i in old:
                by_day.setdefault(str(i.get("at", ""))[:10] or "unknown", []).append(i)
            for day, rows in by_day.items():
                _append_lines(_p("inbox", "archive", day + ".jsonl"), rows)
            gone = {i["id"] for i in old}
            keep = [i for i in queue if i.get("id") not in gone]
            for name, rows in (("queue.jsonl", keep),
                               ("delivered.jsonl", [done[i["id"]] for i in keep if i.get("id") in done])):
                path = _p("inbox", name)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
                os.replace(tmp, path)
            moved = len(old)
    for path in glob.glob(_p("inbox", "archive", "*.jsonl")):
        try:
            if _now() - os.path.getmtime(path) > ARCHIVE_DAYS * 86400:
                os.remove(path)
        except OSError:
            pass
    return moved


# ---- ожидатель (хук Stop и SessionStart, asyncRewake; только при wake_on_mail) ------------------------------

def _warm(transcript):
    last = float(turn().get("last") or 0)
    try:
        last = max(last, os.path.getmtime(transcript)) if transcript else last
    except OSError:
        pass
    return _now() - last < WARM_SEC


def _sig():
    out = []
    for name in ("queue.jsonl", "delivered.jsonl"):
        try:
            st = os.stat(_p("inbox", name))
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return out


def _deliver(kinds):
    try:
        items = take(kinds, max_chars=8500, via="wait")
    except LockBusy:
        return ""
    if not items:
        return ""
    t = turn()  # Рика будим: ход начнётся без UserPromptSubmit
    if not busy_since():
        t.update(busy=True, since=_now(), author="почта (ожидатель)")
    t["last"] = _now()
    save_turn(t)
    voice = sum(1 for i in items if i.get("kind") == "voice")
    head = ("Почта Рика: %d входящих (голос %d, терминалы %d). Это не ошибка: почту приносит ожидатель "
            "tools/rick_bus.py wait, Claude Code подписывает его вывод «Stop hook blocking error». «📥 Голос» — "
            "фраза, которую %s сказал на странице live; «📥 <Проект>» — ответ терминала (rick_bus.py tell)."
            % (len(items), voice, len(items) - voice, owner()))
    return head + "\n" + "\n".join(render(i) for i in items)


def wait_loop(inp, max_sec=WAIT_MAX):
    """Ждать почту. Возвращает (код, текст): 2 и текст — разбудить Рика, 0 — тихо уйти."""
    sid = inp.get("session_id") or ""
    if inp.get("hook_event_name") == "SessionStart":
        claim_session(inp)  # хук mem_hook start идёт параллельно — заявка идемпотентна
    if not wake_on_mail() or inp.get("agent_id") or not is_rick(sid):
        return 0, ""
    me = os.getpid()
    _write_json(_p("state", "waiter.json"), {"pid": me, "session": sid, "event": inp.get("hook_event_name", ""),
                                             "since": _now()})
    transcript = inp.get("transcript_path") or ""
    try:
        housekeep()
    except Exception:
        pass
    deadline, last_sig, first_window = _now() + max_sec, None, None
    quiet_until = _now() + (START_GRACE if inp.get("hook_event_name") == "SessionStart" else 0)
    while _now() < deadline:
        time.sleep(POLL)
        if _now() < quiet_until:
            continue
        if _read_json(_p("state", "waiter.json")).get("pid") != me:
            return 0, ""  # пришёл новый ожидатель
        if rick().get("cli") != sid:
            return 0, ""  # роль Рика ушла другой сессии
        sig = _sig()
        if sig == last_sig and first_window is None:
            continue
        last_sig = sig
        items = pending()
        if any(i.get("kind") == "voice" for i in items):
            text = _deliver(("voice", "window"))
            if text:
                return 2, text
            last_sig = None
            continue
        windows = [float(i.get("t") or 0) for i in items if i.get("kind") == "window"]
        if not windows:
            first_window = None
            continue
        first_window = min(windows)
        if _now() - first_window >= BATCH_SEC and _warm(transcript):
            text = _deliver(("window",))
            if text:
                return 2, text
            last_sig = None
    return 0, ""


# ---- командная строка -------------------------------------------------------------------------------------

def _fmt_t(t):
    return datetime.datetime.fromtimestamp(float(t)).strftime("%d.%m %H:%M") if t else "—"


def state_text():
    rec, t, items = rick(), turn(), pending()
    voice = sum(1 for i in items if i.get("kind") == "voice")
    since = busy_since()
    w = _read_json(_p("state", "waiter.json"))
    who = ("сессия %s" % rec["cli"][:8]) if rec.get("cli") else "не заявлен — запусти claude в папке Рика"
    if rec.get("local"):
        who += " (чат Desktop %s)" % rec["local"]
    lines = [
        "Рик: %s, с %s" % (who, rec.get("claimed_at") or "—"),
        ("Занят с %s (слово: %s)" % (_fmt_t(since), t.get("author") or "?")) if since
        else "Свободен с %s" % _fmt_t(t.get("free_since") or t.get("last")),
        "Недоставленных: %d (голос %d, терминалы %d)" % (len(items), voice, len(items) - voice),
        ("Ожидатель: pid %s жив с %s" % (w.get("pid"), _fmt_t(w.get("since")))) if waiter_alive()
        else "Ожидателя нет (wake_on_mail выключен или Рик не запущен): почта придёт со следующим словом",
    ]
    return "\n".join(lines)


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="rick_bus.py", description="Почта Рика: tell, note, claim, state, pending, wait")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tell", help="ответ терминала Рику одной строкой")
    p.add_argument("--from", dest="src", required=True, help="имя проекта, например Сайт")
    p.add_argument("text", nargs="+")
    p = sub.add_parser("note", help="строка журнала от Рика")
    p.add_argument("text", nargs="+")
    p = sub.add_parser("claim", help="запасной путь: роль Рика — чату Claude Desktop local_…")
    p.add_argument("local")
    sub.add_parser("state", help="Рик, занят ли, недоставленные, ожидатель")
    sub.add_parser("pending", help="недоставленная почта (ничего не помечает)")
    sub.add_parser("wait", help="ожидатель: только из хука Stop/SessionStart")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "tell":
            item = tell(a.src, " ".join(a.text))
            print("В почте Рика: %s" % item["id"])
        elif a.cmd == "note":
            print(note(" ".join(a.text)))
        elif a.cmd == "claim":
            rec = claim(a.local)
            print("Рик — %s (сессия %s)" % (rec["local"], rec["cli"] or "ещё нет: привяжу при первом хуке"))
        elif a.cmd == "state":
            print(state_text())
        elif a.cmd == "pending":
            items = pending()
            print("\n".join(render(i) for i in items) if items else "Почта пуста")
        elif a.cmd == "wait":
            try:  # ожидатель не ломает ход: любая беда — тихий выход 0
                inp = json.loads(sys.stdin.read() or "{}")
                code, text = wait_loop(inp if isinstance(inp, dict) else {})
            except Exception:
                return 0
            if code == 2:
                sys.stderr.write(text + "\n")
            return code
    except (ValueError, LookupError, LockBusy, OSError) as e:
        print("rick_bus: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
