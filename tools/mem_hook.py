#!/usr/bin/env python3
"""Хуки памяти Рика: mem_hook.py <start|prompt|batch|stop> [--quiet], вход — JSON хука со stdin.

- start  (SessionStart): заявка роли Рика (главная сессия в корне Рика); ядро — скелет ответа SKELETON, почта, ⚠,
  «Факты сейчас:» (проекты, живые чаты, что подключено, память), открытые задачи (tasks.brief), memory/NOW.md,
  15 строк журнала (≤ 9 000 знаков).
- prompt (UserPromptSubmit): «занят с …»; строка журнала с автором; напоминание формы FORM; «⚠ без ответа: …», если
  прошлое слово хозяина осталось без ответа (ход упал или прерван); почта (голос → терминалы), ⚠, «Факты сейчас:»
  при пустом NOW.md или раз в 30 мин (между ними — строка «Ждёт хозяина», wait_line), выдержка из
  memory/projects/<Проект>.md по словам,
  «контекст Рика NNNK» при > 350K.
- batch  (PostToolBatch): поручения терминалам (term.py send, tmux send-keys) — в журнал; голос доставлен, а
  rick_voice.py say нет > 45 с — строка-факт.
- stop   (Stop): «свободен»; «Рик → хозяин · ответ» в журнал; ❗/🛑/❓ — задача и строка «Ждёт хозяина», если ход
  их не записал (keep_need); mem_guard.check() → ⚠ в state/; ход дольше минуты — звук Мака, ответ ждёт хозяина — ещё баннер (turn_signal, config.json → notify.long_turn); ничего не выводит.
--quiet: только журнал и состояние — без вывода, почты, mem_guard и архива почты.

Работает только для сессии Рика (state/rick.json); чужие сессии и субагенты — пустой выход (субагент Рика —
только журнал поручений). Любая беда — выход 0 без вывода.
"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_bus as bus  # noqa: E402
import rick_config  # noqa: E402
import tasks  # noqa: E402

LIMIT = 9000
MAIL_MAX = 3500
WARN_MAX = 900
DOSSIER_MAX = 3000
DOSSIER_EVERY = 10 * 60
WARN_EVERY = 2 * 3600
CTX_LIMIT = 350_000
CTX_EVERY = 30 * 60
VOICE_LAG = 45
FACTS_EVERY = 30 * 60
FACTS_MAX = 460
LONG_TURN = 60   # ход дольше — звук после него (config.json → notify.long_turn)
NEED_RX = re.compile(r"👾 СДЕЛАЙ|[❗❓]")
STYLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".claude", "output-styles", "rick.md")


def _form():
    """Скелет и запреты — из стиля «Рик», один текст на всех: start кладёт их раз за чат; стиль и так в системном
    промпте, поэтому каждый ход — только короткое напоминание FORM."""
    try:
        with open(STYLE, encoding="utf-8") as f:
            text = f.read()
        return "Форма ответа (стиль «Рик»):\n" + text[text.index("## Скелет"):text.index("## Тон")].strip()
    except (OSError, ValueError):
        return "Форма ответа — стиль «Рик»: вердикт жирным первой строкой, «Сейчас:», строка состояния последней."


SKELETON = _form()
FORM = ("Форма — стиль «Рик»: нужен шаг хозяина — «👾 СДЕЛАЙ» первой строкой, выше вердикта; вердикт жирным → "
        "«Сейчас:» ответом на вопрос → темы, если дел от двух, не повторяя «Сейчас:» → одна строка состояния "
        "последней. «?» — только в ❓. Без «уйдёт» и «не уйдёт»: «черновик готов — скажи «да»».")
UNANSWERED = "⚠ без ответа: «%s» — прошлый ход кончился без ответа хозяину; ответь на это в этом ходе."
EVENTS = {"start": "SessionStart", "prompt": "UserPromptSubmit", "batch": "PostToolBatch", "stop": "Stop"}


def _now():
    return bus._now()


def _p(*parts):
    return bus._p(*parts)


# ---- словарь проектов ---------------------------------------------------------------------------------------

def _norm(text):
    text = unicodedata.normalize("NFC", str(text)).lower().replace("ё", "е")   # имя папки с диска бывает в NFD
    return " " + " ".join(re.sub(r"[^\w#/.+-]", " ", text).split()) + " "


def keywords():
    """Проекты: каждый memory/projects/<Проект>.md, плюс memory/keywords.json {Проект: {names: [...], words: [...]}}."""
    out = {}
    for path in sorted(glob.glob(_p("memory", "projects", "*.md"))):
        out[os.path.basename(path)[:-3]] = {"names": [], "words": []}
    try:
        with open(_p("memory", "keywords.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        out.update({k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)})
    return out


def display(project, kw=None):
    kw = keywords() if kw is None else kw
    names = (kw.get(project) or {}).get("names") or []
    return names[0] if names else project


def project_by_name(name, kw=None):
    """«Сайт», «rick-Site», «rick-Site-1a2b3c» (имя занято другой папкой), «Site» → ключ проекта или ""."""
    kw = keywords() if kw is None else kw
    raw = re.sub(r"^=|:[\w.]*$", "", str(name or "").strip())   # цель tmux: «=rick-X», «rick-X:», «rick-X:0.1»
    keys = [raw[len("rick-"):]] if raw.startswith("rick-") else [raw]
    if raw.startswith("rick-") and re.search(r"-[0-9a-f]{6}$", keys[0]):
        keys.append(keys[0][:-7])
    for key in filter(None, (_norm(k).strip() for k in keys)):
        for proj, v in kw.items():
            # имя сессии term.py: точки, двоеточия и пробелы → «_»
            forms = {_norm(x).strip() for x in [proj] + list(v.get("names") or [])}
            forms |= {_norm(re.sub(r"[.:\s]", "_", proj)).strip()}
            if key in forms:
                return proj
    return ""


def target_name(target, kw=None):
    target = re.sub(r"^=|:[\w.]*$", "", str(target or "").strip())
    proj = project_by_name(target, kw)
    return display(proj, kw) if proj else (re.sub(r"^rick-", "", target) or "?")


def match_projects(text, kw=None, first=""):
    """До двух проектов по словам; терминал-автор — первым."""
    kw = keywords() if kw is None else kw
    norm = _norm(text)
    scores = {}
    for proj, v in kw.items():
        score = 0
        words = [(n, 2) for n in [proj] + list(v.get("names") or [])] + [(w, 1) for w in v.get("words") or []]
        for w, weight in words:
            stem = str(w).lower().replace("ё", "е")
            if not stem.strip():
                continue
            if " " in stem or not stem[0].isalnum():
                hit = stem in norm
            else:
                hit = re.search(r"(?<![\w])" + re.escape(stem), norm) is not None
            score += weight if hit else 0
        if score:
            scores[proj] = score
    order = sorted(scores, key=lambda p: -scores[p])
    if first in kw:
        order = [first] + [p for p in order if p != first]
    return order[:2]


# ---- автор входящего ----------------------------------------------------------------------------------------

_PEER = re.compile(r'<cross-session-message\s+from="([^"]*)"(?:\s+name="([^"]*)")?[^>]*>\s*(.*?)\s*'
                   r'(?:</cross-session-message>|$)', re.S)
_AGENT = re.compile(r'<agent-message\s+from="([^"]*)"[^>]*>\s*(.*?)\s*(?:</agent-message>|$)', re.S)
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)
_SCHED = re.compile(r'<scheduled-task\s+name="([^"]*)"')
_ORCH = re.compile(r"^Оркестратор\b[^:\n]{0,80}:\s*")
_PASTED = re.compile(r"</?pasted_content[^>]*>")


def _prefix(owner):
    """«<Проект>: …» или «<Проект> ждёт <хозяина>: …» — вставка терминала в поле Рика."""
    return re.compile(r"^\W*([^\s:«»\"<>]{2,30}(?: [^\s:«»\"<>]{2,30}){0,2}?)(\s+ждёт (?:%s|хозяина))?:\s"
                      % re.escape(owner), re.I)


def author(prompt, kw=None):
    """→ {"who": «[чел] <хозяин>», "word": чьё слово, "kind": owner|window|agent|mail|service|unknown,
    "project": ключ проекта или "", "text": суть, "via": источник}."""
    kw = keywords() if kw is None else kw
    owner = bus.owner()
    raw = str(prompt or "").strip()
    text = _PASTED.sub("", raw).strip()
    res = {"who": "[чел] " + owner, "word": owner, "kind": "owner", "project": "", "text": text, "via": "чат Рика"}
    if text.startswith("Another Claude session sent a message"):
        m, a = _PEER.search(text), _AGENT.search(text)
        if m:
            proj = project_by_name(m.group(2) or "", kw)
            name = display(proj, kw) if proj else (m.group(2) or m.group(1)[:14] or "сессия")
            res.update(who="[агент] " + name, word=name, kind="window", project=proj, text=m.group(3),
                       via="send_message")
        elif a:
            body = a.group(2)
            if "follows:" in body[:900]:
                body = body.split("follows:", 1)[1]
            res.update(who="[агент] субагент Рика", word="субагент Рика", kind="agent", text=body,
                       via="отчёт субагента")
        else:
            res.update(who="автор?", word="автор?", kind="unknown", via="сообщение сессии")
    elif text.startswith("<task-notification>") and "Почта Рика:" in text and "📥" in text:
        # принёс ожидатель, строки уже в журнале. Кто — по первой строке «📥»: текст письма идёт внутри «…»
        # одной строкой, имя терминала без «:» и «голос» — поэтому начало строки письмо подделать не может.
        m = re.search(r"(?m)^📥 ([^:\n]+): «", text)
        first = m.group(1) if m else ""
        word = (owner + " (голос)") if first == "Голос" else (first or "почта")
        res.update(who="[агент] ожидатель", word=word, kind="mail", text="почта", via="ожидатель")
    elif text.startswith("<task-notification>"):
        m = _SUMMARY.search(text)
        res.update(who="служебное", word="служебное", kind="service", text="фон: " + (m.group(1) if m else ""),
                   via="task-notification")
    elif text.startswith("<scheduled-task"):
        m = _SCHED.search(text)
        res.update(who="служебное", word="расписание", kind="service",
                   text="расписание " + (m.group(1) if m else "?"), via="scheduled-task")
    elif text.startswith("<bash-input>"):
        res.update(text="команда: " + re.sub(r"<[^>]+>", " ", text), via="!-команда")
    elif text.startswith("<"):
        res.update(who="автор?", word="автор?", kind="unknown", via="?")
    elif _ORCH.match(text):
        res.update(who="[агент] Оркестратор", word="Оркестратор", kind="agent", text=_ORCH.sub("", text, 1),
                   via="вставка")
    else:
        m = _prefix(owner).match(text)
        if m:
            proj = project_by_name(m.group(1), kw)
            if proj:
                name = display(proj, kw)
                body = text[m.end():]
                res.update(who="[агент] " + name, word=name, kind="window", project=proj,
                           text=("ждёт хозяина: " + body) if m.group(2) else body, via="вставка в поле")
            elif m.group(2):  # «Кто-то ждёт хозяина:» — не хозяин, но и не знакомый проект
                res.update(who="автор?", word="автор?", kind="unknown", via="вставка в поле")
    return res


# ---- память проектов и хвосты -------------------------------------------------------------------------------

def dossier_excerpt(project, limit=DOSSIER_MAX):
    """Шапка + «Сейчас» + «Ждёт …» + 10 строк «Хроники» из memory/projects/<Проект>.md."""
    try:
        with open(_p("memory", "projects", project + ".md"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    head, sections, cur = [], [], None
    for line in lines:
        if line.startswith("## "):
            cur = [line]
            sections.append(cur)
        elif cur is None:
            head.append(line)
        else:
            cur.append(line)
    out = ["Проект %s (memory/projects/%s.md, выдержка):" % (project, project)] + [h for h in head[:2] if h.strip()]
    for want in (("Сейчас", "Состояние"), ("Ждёт",), ("Хроника",)):
        for sec in sections:
            title = sec[0][3:].strip()
            if title.startswith(want):
                body = [s for s in sec[1:] if s.strip()]
                if want == ("Хроника",):
                    body = [s for s in body if s.lstrip().startswith("-")][:10]
                out += [sec[0]] + body
                break
    text = "\n".join(out)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…"
    return text


def journal_tail(n=15):
    lines = []
    day = datetime.datetime.fromtimestamp(_now())
    for back in (0, 1):
        path = _p("journal", (day - datetime.timedelta(days=back)).strftime("%Y-%m-%d") + ".md")
        try:
            with open(path, encoding="utf-8") as f:
                got = [ln.rstrip("\n") for ln in f if ln.startswith("- ")]
        except OSError:
            continue
        lines = got[-(n - len(lines)):] + lines if got else lines
        if len(lines) >= n:
            break
    return lines[-n:]


def context_tokens(transcript):
    """Контекст Рика по последнему ответу модели в хвосте транскрипта (последние 400 КБ)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return 0
    for line in reversed(tail):
        if '"usage"' not in line:
            continue
        try:
            u = json.loads(line)["message"]["usage"]
            return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
                int(u.get("cache_creation_input_tokens") or 0)
        except (ValueError, KeyError, TypeError):
            continue
    return 0


# ---- факты: проекты, чаты, что подключено --------------------------------------------------------------------
# Модель на короткую фразу инструменты не зовёт и выдумывает «проектов ноль» — факты кладёт хук.

def _term(cmd):
    """→ (код, вывод) term.py <cmd> в этом же процессе (без запуска python — хук укладывается в секунду)."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    try:
        import term
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = term.main([cmd])
    except BaseException as e:  # noqa: B036 — argparse выходит SystemExit; хук не падает ни от чего
        return 1, "%s: %s" % (type(e).__name__, e)
    return code or 0, (out.getvalue() if not code else err.getvalue() or out.getvalue()).strip()


def _why(text):
    return bus.one_line(re.sub(r"^term\.py:\s*", "", text.splitlines()[-1] if text else "?"), 120)


def facts_block():
    """Блок «Факты сейчас:» (≤ FACTS_MAX знаков) — проекты (term.py projects), живые чаты (term.sessions, ⚠ вопрос на
    экране), что подключено (rick_config.status), память; имена проектов — на остаток места; wait_line() — сверх
    FACTS_MAX."""
    code, out = _term("projects")
    names = [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []
    lines = []
    try:
        import term
        chats = ", ".join("%s%s" % (s[len(term.PREFIX):], " (ждёт ответа)" if term.question(s) else "")
                          for s in sorted(term.sessions()) if s.startswith(term.PREFIX)) or "нет"
    except Exception as e:
        chats = "не узнать — %s" % type(e).__name__
    lines.append(bus.one_line("Живые чаты проектов: " + chats, 110))
    try:
        import rick_config
        st = dict(ln.split(": ", 1) for ln in rick_config.status() if ": " in ln)
        cut = lambda v: re.sub(r"\s*\(.*?\)", "", v.split(" — ")[0]).strip()  # noqa: E731
        lines.append("Telegram: %s · Напоминания: %s · озвучка ответов: %s · разговор голосом: %s" % (
            cut(st.get("Telegram от твоего аккаунта", "?")), cut(st.get("Напоминания", "?")),
            cut(st.get("Озвучка ответов", "?")), cut(st.get("Разговор голосом (страница live)", "?"))))
    except Exception as e:
        lines.append("Подключено: не узнать — %s" % type(e).__name__)
    lines.append(memory_line())
    head = "Факты сейчас (%s, проверено только что — отвечай по ним):\n" % time_hm()
    room = max(40, min(140, FACTS_MAX - len(head) - sum(len(x) + 1 for x in lines)))
    lines.insert(0, bus.one_line("Проекты: %d%s" % (len(names), " — " + ", ".join(names) if names else ""), room)
                 if code == 0 else "Проекты: 0 — " + _why(out))
    wait = wait_line()
    return (head + "\n".join(lines))[:FACTS_MAX] + ("\n" + wait if wait else "")


WAIT_TAIL = " — последней темой ответа: «Ждёт тебя: …»"


def wait_line():
    """«Ждёт хозяина: … — последней темой ответа: «Ждёт тебя: …»» из memory/NOW.md (≤ 260) или "": висит ❗ — модель
    видит его и с хвостом-указанием не теряет, когда хозяин спрашивает о другом."""
    wait = "; ".join(ln[2:].strip() for ln in now_items("Ждёт"))
    return bus.one_line("Ждёт хозяина: " + wait, 260 - len(WAIT_TAIL)) + WAIT_TAIL if wait else ""


def memory_line():
    """«Память: …» — что на диске на самом деле: без неё модель пересказывает описание памяти как факт."""
    names = sorted(os.path.basename(p)[:-3] for p in glob.glob(_p("memory", "projects", "*.md")))
    try:
        with open(_p("journal", datetime.datetime.fromtimestamp(_now()).strftime("%Y-%m-%d") + ".md"),
                  encoding="utf-8") as f:
            day = sum(1 for ln in f if ln.startswith("- "))
    except OSError:
        day = 0
    return "Память: досье — %d (%s), строк в «что сейчас» — %d, журнал за сегодня — %d" % (
        len(names), bus.one_line(", ".join(names), 30) or "ни одного", len(now_items()), day)


def time_hm():
    return datetime.datetime.fromtimestamp(_now()).strftime("%H:%M")


def now_items(section=""):
    """Строки «- » memory/NOW.md, кроме «(пусто…»; section — только из разделов «## <section>…»."""
    try:
        with open(_p("memory", "NOW.md"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    cur, out = "", []
    for ln in lines:
        if ln.startswith("## "):
            cur = ln[3:].strip()
        elif ln.startswith("- ") and not ln[2:].lstrip().startswith("(пусто") and cur.startswith(section):
            out.append(ln)
    return out


def now_empty():
    """memory/NOW.md нет или в нём только шаблон."""
    return not now_items()


# ---- ⚠ и частота --------------------------------------------------------------------------------------------

def shown():
    return bus._read_json(_p("state", "shown.json"))


def save_warnings(lines):
    old = bus._read_json(_p("state", "warnings.json")).get("items") or {}
    items = {}
    for line in [str(x) for x in lines if str(x).strip()][:5]:
        items[line] = old.get(line) or {"first": _now(), "shown": 0}
    bus._write_json(_p("state", "warnings.json"), {"items": items, "checked": _now()})


def due_warnings():
    data = bus._read_json(_p("state", "warnings.json"))
    items = data.get("items") or {}
    due = [k for k, v in items.items() if _now() - float((v or {}).get("shown") or 0) >= WARN_EVERY]
    if due:
        for k in due:
            items[k]["shown"] = _now()
        data["items"] = items
        bus._write_json(_p("state", "warnings.json"), data)
    return due


def _mail(kinds, budget, via):
    if budget < 300:
        return ""
    try:
        items = bus.take(kinds, max_chars=budget - 120, via=via)
    except bus.LockBusy:
        return ""
    if not items:
        return ""
    voice = sum(1 for i in items if i.get("kind") == "voice")
    return "Почта Рика: %d входящих (голос %d, терминалы %d).\n%s" % (
        len(items), voice, len(items) - voice, "\n".join(bus.render(i) for i in items))


def _join(parts):
    text = "\n\n".join(p for p in parts if p)
    return text[:LIMIT]


# ---- хуки ---------------------------------------------------------------------------------------------------

def start(inp, quiet):
    if inp.get("agent_id"):
        return ""
    bus.claim_session(inp)
    if not bus.is_rick(inp.get("session_id")):
        return ""
    t = bus.turn()
    t["last"] = _now()
    miss = t.pop("unanswered", "")
    bus.save_turn(t)
    if quiet:
        return ""
    wake = bus.wake_on_mail()
    parts = ["Ядро памяти Рика (начало чата, %s). Почта Рика — строки «📥 …»: «📥 Голос» — фраза, сказанная "
             "хозяином (%s) на странице live; «📥 <Проект>» — ответ терминала (rick_bus.py tell). %s"
             % (inp.get("source") or "старт", bus.owner(),
                "В простое почту приносит ожидатель rick_bus, Claude Code показывает её как «Stop hook blocking "
                "error» в task-notification." if wake else "Почта приходит со словом хозяина (wake_on_mail выключен).")]
    parts.append(SKELETON)
    parts.append(UNANSWERED % miss if miss else "")
    parts.append(_mail(("window",) if wake else ("voice", "window"), MAIL_MAX, "start"))
    warn = due_warnings()
    if warn:
        parts.append("\n".join(warn)[:WARN_MAX])
    parts.append(facts_block())
    parts.append(tasks.brief())
    st = shown()
    st["facts"] = _now()
    bus._write_json(_p("state", "shown.json"), st)
    try:
        with open(_p("memory", "NOW.md"), encoding="utf-8") as f:
            now_text = f.read().strip()
    except OSError:
        now_text = ("memory/NOW.md нет — заведи из шаблона: cp memory/NOW.example.md memory/NOW.md "
                    "(или ./install.sh) и веди по строке на проект.")
    tail = journal_tail(15)
    used = sum(len(p) + 2 for p in parts if p)
    room = LIMIT - used - 80
    tail_text = "\n".join(tail)
    now_room = max(500, room - min(len(tail_text), 3000))
    if len(now_text) > now_room:
        now_text = now_text[:now_room].rsplit("\n", 1)[0] + "\n…"
    parts.append("== memory/NOW.md ==\n" + now_text)
    if tail:
        room = LIMIT - sum(len(p) + 2 for p in parts if p) - 60
        while tail and len("\n".join(tail)) > room:
            tail = tail[1:]
        parts.append("== journal/, последние %d строк ==\n%s" % (len(tail), "\n".join(tail)))
    return _join(parts)


def prompt(inp, quiet):
    if inp.get("agent_id") or not bus.is_rick(inp.get("session_id")):
        return ""
    kw = keywords()
    a = author(inp.get("prompt") or "", kw)
    now = _now()
    t = bus.turn()
    miss = t.pop("unanswered", "")
    t.update(busy=True, since=now, last=now, author=a["word"], wait=now_items("Ждёт"))
    if a["kind"] == "owner" and a["via"] == "чат Рика":  # слово хозяина ждёт ответа до хука stop
        t["unanswered"] = bus.one_line(a["text"], 200)
    bus.save_turn(t)
    if a["kind"] != "mail":  # почту ожидателя журнал уже знает (tell, tell_voice)
        bus.journal("%s → [агент] Рик · %s · %s" % (a["who"], bus.one_line(a["text"], 300) or "—", a["via"]))
    if quiet:
        return ""
    parts = [FORM, UNANSWERED % miss if miss else "", _mail(("voice", "window"), MAIL_MAX, "prompt")]
    facts = due_warnings()
    st = shown()
    if now - float(st.get("ctx") or 0) >= CTX_EVERY:
        tok = context_tokens(inp.get("transcript_path"))
        if tok > CTX_LIMIT:
            facts.append("Контекст Рика %dK (по последнему ответу модели; больше 350K — пора /compact на ближайшей "
                         "границе)." % (tok // 1000))
            st["ctx"] = now
    if facts:
        parts.append("\n".join(facts)[:WARN_MAX])
    if now_empty() or now - float(st.get("facts") or 0) >= FACTS_EVERY:
        parts.append(facts_block())
        st["facts"] = now
    else:
        parts.append(wait_line())   # висящее ❗ — каждый ход, не раз в FACTS_EVERY
    if a["kind"] not in ("service", "mail"):
        seen = st.get("dossier") or {}
        for proj in match_projects(a["text"], kw, first=a["project"]):
            if now - float(seen.get(proj) or 0) < DOSSIER_EVERY:
                continue
            room = LIMIT - sum(len(p) + 2 for p in parts if p)
            text = dossier_excerpt(proj, min(DOSSIER_MAX, room))
            if len(text) > 200:
                parts.append(text)
                seen[proj] = now
        st["dossier"] = seen
    bus._write_json(_p("state", "shown.json"), st)
    return _join(parts)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_ORDER = re.compile(r"\bterm\.py['\"]?\s+(send)(?=\s)|\btmux\s+send-keys(?=\s)")
_SAY = re.compile(r"rick_voice\.py['\"]?\s+say\b")
_STOP_TOK = {"|", "||", "&&", ";", "&", "2>&1", ">", ">>", "2>", "<"}
_VALUE_OPTS = {"-t", "-n"}


def _split_heredocs(cmd):
    """→ [(строка команды, тело heredoc)]: тела heredoc — не команды."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        line, body = lines[i], []
        m = _HEREDOC.search(line)
        if m:
            i += 1
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
        out.append((line, "\n".join(body)))
        i += 1
    return out


def bash_orders(cmd, kw=None):
    """Поручения терминалам в команде Bash: term.py send, tmux send-keys → [(кому, текст, как)]."""
    import shlex
    res = []
    for line, body in _split_heredocs(str(cmd or "")):
        for m in _ORDER.finditer(line):
            sub = m.group(1) or "send-keys"
            rest = line[m.end():]
            try:
                toks = shlex.split(rest)
            except ValueError:
                toks = rest.split()
            cut = next((i for i, tok in enumerate(toks) if tok in _STOP_TOK or tok.startswith((">", "2>"))), len(toks))
            semi = next((i for i, tok in enumerate(toks[:cut]) if tok.endswith(";")), None)  # «Enter; done»
            if semi is not None:
                toks[semi], cut = toks[semi].rstrip(";"), semi + 1
            toks, opts, pos, i = toks[:cut], {}, [], 0
            while i < len(toks):
                tok = toks[i]
                if tok in _VALUE_OPTS and i + 1 < len(toks):
                    opts[tok] = toks[i + 1]
                    i += 2
                    continue
                if not (tok.startswith("-") and tok != "-"):
                    pos.append(tok)
                i += 1
            if sub == "send-keys":
                target = opts.get("-t", "")
                text = " ".join(p for p in pos if p not in ("Enter", "C-m", "KPEnter"))
                if not text:
                    continue  # отдельный Enter — не поручение
            else:  # term.py send <Проект> "<текст>"
                target, text = (pos[0] if pos else ""), (pos[1] if len(pos) > 1 else "")
            if text == "-":
                text = body
            via = "tmux send-keys" if sub == "send-keys" else "term.py send"
            if "$" in target:  # цикл по терминалам (for s in rick-A rick-B …): имена — из списка цикла
                names = list(dict.fromkeys(target_name(n, kw) for n in re.findall(r"\brick-[\w.-]+", str(cmd))))
                if any(r[1] == "терминалы: " + ", ".join(names) or r[0] == "терминалы" for r in res):
                    continue  # один цикл — одна строка
                target = "терминалы: " + ", ".join(names) if names else "терминалы"
                if "$" in text:
                    text = "текст из переменной или файла"
                res.append((target, text, via + " в цикле"))
                continue
            res.append((target_name(target, kw), text, via))
    return res


def batch(inp, quiet):
    if not bus.is_rick(inp.get("session_id")):  # субагент Рика — тоже сюда, но только журнал
        return ""
    kw = keywords()
    agent = inp.get("agent_id")
    t = bus.turn()
    who = "[агент] субагент Рика" if agent else "[агент] Рик"
    said, orders = False, []
    for call in inp.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        ti = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
        if str(call.get("tool_name") or "") == "Bash":
            cmd = str(ti.get("command") or "")
            orders += bash_orders(cmd, kw)
            said = said or any(_SAY.search(line) for line, _ in _split_heredocs(cmd))
    for target, text, via in orders:
        bus.journal("%s → [агент] %s · поручение: %s · по слову: %s (%s)" % (
            who, target, bus.one_line(text, 300) or "—", t.get("author") or "?", via))
    if agent:
        return ""
    now = _now()
    if not bus.busy_since():
        t.update(busy=True, since=now)
    t["last"] = now
    if said:
        t["said_at"] = now
    out = ""
    vt = bus.voice_taken()
    if vt and vt >= float(t.get("since") or 0) - 5 and float(t.get("said_at") or 0) < vt \
            and now - vt > VOICE_LAG and t.get("voice_warned") != vt:
        out = "Голос хозяина ждёт %d с; rick_voice.py say в этом ходе не вызывался." % int(now - vt)
        t["voice_warned"] = vt
    bus.save_turn(t)
    return "" if quiet else out


def turn_signal(msg):
    """Ход дольше LONG_TURN кончился: звук Мака; ответ ждёт хозяина (👾 СДЕЛАЙ, ❗, ❓) — notify.py, баннер и звук.
    Выключается в config.json: notify.long_turn (или notify.sound / notify.banner). Фоном — хук не ждёт."""
    cfg = rick_config.load().get("notify") or {}
    if not cfg.get("long_turn"):
        return
    lines = [x for x in msg.splitlines()[:12] if x.strip()] if isinstance(msg, str) else []
    if lines and NEED_RX.search(lines[0]):   # первая строка — «👾 СДЕЛАЙ» или вердикт ❗/❓
        text = next((x for x in lines if re.search("[❗❓]", x)), lines[0])
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py"),
               bus.one_line(text.replace("*", "").lstrip("# "), 200)]
    elif cfg.get("sound"):
        cmd = [os.environ.get("RICK_AFPLAY") or "afplay", "/System/Library/Sounds/Glass.aiff"]
    else:
        return
    _spawn(cmd)


def _spawn(cmd):
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


VERDICT_RX = re.compile(r"^\*\*\s*([❗🛑❓])\ufe0f?\s*(.+?)\*\*", re.M)
NOW_RX = re.compile(r"^Сейчас:\s*(.+)$", re.M)


def owner_word():
    """Последнее слово хозяина из журнала — чат или голос: «· [чел] <хозяин> → [агент] Рик · <слова> · <откуда>»."""
    rx = re.compile(r"· \[чел\] %s(?: \(голос\))? → \[агент\] Рик · (.+) · [^·]+$" % re.escape(bus.owner()))
    return next((m.group(1) for m in map(rx.search, reversed(journal_tail(15))) if m), "")


def add_waiting(item):
    """«- item» в раздел «## Ждёт хозяина» memory/NOW.md: «(пусто)» уходит, раздела нет — заводится."""
    path = _p("memory", "NOW.md")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = ["# Сейчас"]
    head = next((i for i, ln in enumerate(lines) if ln.startswith("## Ждёт")), None)
    if head is None:
        lines += ["", "## Ждёт хозяина"]
        head = len(lines) - 1
    end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    lines = [ln for i, ln in enumerate(lines) if not (head < i < end and ln.startswith("- (пусто"))]
    end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = max([i for i in range(head + 1, end) if lines[i].startswith("- ")], default=head)
    lines.insert(last + 1, "- " + item)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)


def keep_need(msg, t):
    """Ответ ❗/🛑/❓ — дело в памяти в этом же ходе: задача #N (названная в вердикте, заведённая в ходе или новая) и
    строка «<знак> <дело> (#N)» в «Ждёт хозяина», если ход её не дописал. Дело — имя задачи, слово хозяина этого хода
    (из журнала, усечённо), иначе «Сейчас:» ответа, вердикт — только если нет обоих: колкость вердикта в память не кладётся. Пишет
    хук: на такой ход модель отвечает по фактам без инструментов, и правило «запиши в том же ходе» не исполнялось."""
    m = VERDICT_RX.search(msg) if isinstance(msg, str) else None
    if not m or not t.get("since"):
        return
    sign, verdict = m.group(1), bus.one_line(m.group(2), 120)
    rows = tasks.load()
    mark = datetime.datetime.fromtimestamp(float(t["since"]) - 1).isoformat(timespec="seconds")
    named = [n for n, r in rows.items()
             if r.get("status") in ("open", "doing") and re.search(r"#%d(?!\d)" % n, verdict)]
    n = (named or [n for n, r in rows.items() if r.get("created", "") >= mark] or [0])[0]
    word = owner_word() if str(t.get("author") or "").startswith(bus.owner()) else ""
    now_m = NOW_RX.search(msg)
    what = bus.one_line((rows[n].get("title") if n else "") or word or (now_m.group(1) if now_m else "") or verdict,
                        120)   # задача уже есть — её имя; иначе слово хозяина, «Сейчас:», вердикт последним
    if not n:
        kw = keywords()
        proj = match_projects(word, kw)
        n = tasks.add([{"title": what, "project": display(proj[0], kw) if proj else "Рик", "original": word}])[0]["id"]
    wait = now_items("Ждёт")
    if ("wait" in t and wait != t["wait"]) or any(what in ln or re.search(r"#%d(?!\d)" % n, ln) for ln in wait):
        return   # ход сам записал или строка уже есть
    add_waiting("%s %s (#%d)" % (sign, what, n))


def stop(inp, quiet):
    if inp.get("agent_id") or not bus.is_rick(inp.get("session_id")):
        return ""
    t = bus.turn()
    msg = inp.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        bus.journal("[агент] Рик → [чел] %s · ответ: %s · ход на слово: %s" % (
            bus.owner(), bus.one_line(msg, 200), t.get("author") or "?"))
        t.pop("unanswered", None)
    now = _now()
    long_turn = t.get("busy") and now - float(t.get("since") or now) >= LONG_TURN
    t.update(busy=False, free_since=now, last=now)
    bus.save_turn(t)
    if quiet:  # только журнал и состояние
        return ""
    if long_turn:
        turn_signal(msg)
    warns = []
    try:
        keep_need(msg, t)
    except Exception as e:
        warns.append("⚠ ответ с ❗/🛑/❓ не записан в память (%s): строка в «Ждёт хозяина» и tasks.py add" % type(e).__name__)
    try:
        import mem_guard  # check(home=None, now=None) -> list[str]
        warns += mem_guard.check(bus.HOME)
    except Exception:
        pass
    try:
        save_warnings(warns)
    except Exception:
        pass
    try:
        bus.housekeep()
    except Exception:
        pass
    return ""


def main(argv):
    try:
        mode = argv[1] if len(argv) > 1 else ""
        fn = {"start": start, "prompt": prompt, "batch": batch, "stop": stop}.get(mode)
        raw = sys.stdin.read()
        if not fn or not raw.strip():
            return 0
        inp = json.loads(raw)
        if not isinstance(inp, dict):
            return 0
        out = fn(inp, "--quiet" in argv[2:])
        if out:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": EVENTS[mode],
                                                                "additionalContext": out[:LIMIT]}},
                                        ensure_ascii=False))
    except Exception as e:  # хук не ломает ход
        try:
            os.makedirs(_p("state"), exist_ok=True)
            with open(_p("state", "hook-errors.log"), "a", encoding="utf-8") as f:
                f.write("%s %s %s: %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"),
                                            argv[1:2], type(e).__name__, e))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
