#!/usr/bin/env python3
"""Терминальные чаты проектов в tmux: Рик поручает, читает и следит за контекстом.

  term.py up <Проект|путь> [--task файл] [--effort high]  — чат проекта в tmux rick-<Проект>, строка в NOW.md, досье
  term.py send <Проект> "<текст>|-"                        — поручение: текст, Enter, проверка «дошло»
  term.py read <Проект> [-n 60]                            — последние строки окна
  term.py stop <Проект|путь>                               — погасить чат этой папки (смена чата — только так)
  term.py list                                             — живые чаты: контекст, простой, ⚠ ждёт ответа
  term.py watch [--max 45]                                 — фоном: выйдет 0, как только чат встанет на вопрос
  term.py projects                                         — папки проектов из projects_root

Проект — папка projects_root/<имя> из config.json или путь (с / ~ .); чат ищется по папке на диске, а не по
имени: имя сессии Рик сам не собирает. Подробно — docs/terminals.md. Подмены для тестов: RICK_TMUX, RICK_CLAUDE,
RICK_CLAUDE_PROJECTS, RICK_TERM_WAIT.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402

PREFIX, CTX_LIMIT = "rick-", 300_000
TRUST_RX = re.compile(r"trust (the files in )?this folder|project you created or one you trust", re.I)
BYPASS_RX = re.compile(r"Bypass Permissions mode", re.I)
AUTO_RX = re.compile(r"Enable auto mode\?", re.I)   # отдельный диалог: ни BYPASS_RX, ни ASK_RX его не ловят
ASK_RX = re.compile(r"Do you want to |Would you like to ", re.I)
RULE_RX = re.compile(r"^\s*[─━-]{20,}\s*$")
CLAUDE_FILES = (".claude", ".mcp.json")   # чужие хуки, MCP, skills/agents/commands с allowed-tools
TRUST = "доверие к папке"
# диалоги первого запуска: вид, признак, пункт «да», нужный permission_mode («make it my default» — не жмём)
DIALOGS = (("bypass", BYPASS_RX, "Yes, I accept", "bypassPermissions"), ("auto", AUTO_RX, "Yes, enable auto mode", "auto"),
           (TRUST, TRUST_RX, "Yes", None))
GONE_RX = re.compile(r"no server running|error connecting to|can't find (session|pane|window)|session not found", re.I)
UP_GONE = "чат проекта закрылся сразу после запуска — похоже, Claude не вошёл в аккаунт: открой Терминал и набери claude"
GONE = "чат закрылся — проверь term.py list, поднять заново: term.py up <Проект>"


class TermError(Exception):
    """Отказ с понятной причиной: печатается и выходит кодом 2."""


class Gone(TermError):
    """Сессии tmux больше нет — чат закрылся; сырую ошибку tmux наружу не отдаём."""


def tmux(*args, check=True):
    exe = os.environ.get("RICK_TMUX") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    done = subprocess.run([exe, *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        err = (done.stderr or "").strip()
        raise Gone(GONE) if GONE_RX.search(err) else TermError("tmux %s: %s" % (args[0], err or done.returncode))
    return done


def wait(k=1.0):
    time.sleep(float(os.environ.get("RICK_TERM_WAIT", "1.5")) * k)


def nfc(s):
    return unicodedata.normalize("NFC", s)


def same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def session(name):
    """rick-<Проект>: Unicode остаётся, tmux не терпит в имени только точку, двоеточие и пробелы."""
    name = name[len(PREFIX):] if name.lower().startswith(PREFIX) else name
    return PREFIX + re.sub(r"[.:\s]", "_", nfc(name))


def sessions():
    """{сессия: её папка} по tmux list-sessions."""
    out = tmux("list-sessions", "-F", "#{session_name}\t#{session_path}", check=False).stdout
    return dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)


def own_session(name, folder):
    """Сессия этой папки — любая живая rick-*, чья папка та же на диске (регистр, NFD/NFC, ссылка — не важны);
    нет — rick-<Проект>, а занято чатом другой папки — rick-<Проект>-<хеш пути>."""
    have = sessions()
    mine = sorted(s for s, path in have.items() if s.startswith(PREFIX) and same(path, folder))
    if mine:
        return mine[0]   # базовое имя сортируется раньше хешевого
    base = session(name)
    return base if base not in have else base + "-" + hashlib.sha1(
        os.path.realpath(folder).encode("utf-8")).hexdigest()[:6]


def inside(folder, root):
    folder, root = os.path.realpath(folder), os.path.realpath(root)
    return folder != root and os.path.commonpath([folder, root]) == root


def project(raw, cfg):
    """(имя, папка) — как на диске: «demo.app» на APFS — та же папка «Demo.app», имя в NFC."""
    if not (raw := (raw or "").strip()):
        raise TermError("проект не назван")
    path = os.path.expanduser(raw) if raw.startswith(("/", "~", ".")) else os.path.join(cfg["projects_root"], raw)
    if not os.path.isdir(folder := os.path.abspath(path)):
        raise TermError("папки %s нет — ничего не открыл" % folder)
    parent, base = os.path.split(folder)
    disk = sorted((e != base, e) for e in (os.listdir(parent) if os.access(parent, os.R_OK) else [])
                  if nfc(e).casefold() == nfc(base).casefold())
    folder = next((os.path.join(parent, e) for _, e in disk if same(os.path.join(parent, e), folder)), folder)
    return nfc(os.path.basename(folder)), folder


def exact(sess):
    """Цель tmux для человека: «=» — только это имя, в кавычках — zsh иначе раскрыл бы «=слово»."""
    q = shlex.quote("=" + sess)
    return q if q.startswith("'") else "'%s'" % q


def alive(sess):
    return tmux("has-session", "-t", "=" + sess, check=False).returncode == 0


def live(raw, cfg):
    """Живая сессия проекта; чат с тем же именем, но другой папки — не она."""
    try:
        sess = own_session(*project(raw, cfg))
    except TermError:   # папки нет — чат по имени, только если он не чат другой, живой папки
        sess, want = session(raw), os.path.abspath(os.path.expanduser(raw) if raw.strip().startswith(("/", "~", "."))
                                                 else os.path.join(cfg["projects_root"], raw.strip()))
        if os.path.exists(path := sessions().get(sess, "")) and os.path.abspath(path) != want:
            raise TermError("чата этой папки нет: %s — чат папки %s" % (sess, path))
    if not alive(sess):
        raise TermError("чата %s нет — подними: term.py up %s" % (sess, raw))
    return sess


def capture(sess, lines=None):
    """Видимый экран (lines=None) или последние lines строк вместе с историей."""
    out = tmux("capture-pane", "-p", "-J", "-t", "=" + sess + ":", *(["-S", "-%d" % lines] if lines else []))
    return "\n".join(out.stdout.rstrip("\n").splitlines()[-(lines or 0):])


def keys(sess, *k):
    tmux("send-keys", "-t", "=" + sess + ":", *k)


def bash_rule(command):
    """Правило Claude Code Bash(<команда>): «\\», «(» и «)» внутри — с обратной косой, как пишет сам Claude Code."""
    return "Bash(%s)" % command.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def cmd_up(args, cfg):
    name, folder = project(args.project, cfg)
    if alive(sess := own_session(name, folder)):
        return print("уже есть %s — сменить: term.py stop %s, потом up" % (sess, shlex.quote(args.project))) or 0
    home, state = rick_config.home(), os.path.join(rick_config.home(), "state")
    reply = "python3 %s tell" % shlex.quote(os.path.join(home, "tools", "rick_bus.py"))   # та же строка — в правиле
    task = ('Отвечай Рику одной строкой («готово», «ждёт хозяина: …» или план) ровно этой командой, путь буквально: '
            '%s --from %s "…" (не сработала — python3 "$RICK_HOME/tools/rick_bus.py" tell --from %s "…"). '
            'Сначала прочитай памятку чата проекта: %s'
            % (reply, shlex.quote(name), shlex.quote(name), shlex.quote(os.path.join(home, "docs", "project-chat.md"))))
    if args.task:
        with open(args.task, encoding="utf-8") as fh:
            task += "\n" + fh.read().strip()
    os.makedirs(state, exist_ok=True)
    task_file, launch, perms = (os.path.join(state, f % sess) for f in ("task-%s.txt", "launch-%s.sh", "settings-%s.json"))
    with open(task_file, "w", encoding="utf-8") as fh:
        fh.write(task + "\n")
    cmd = [os.environ.get("RICK_CLAUDE") or shutil.which("claude") or "claude"]
    for flag, value in (("--model", cfg.get("model")), ("--effort", args.effort or cfg.get("effort")),
                        ("--permission-mode", cfg.get("permission_mode")), ("--name", sess)):
        cmd += [flag, value] if value and value != "default" else []
    # Правила — файлом --settings, не строкой --allowedTools: CLI режет её по пробелам и запятым вне скобок
    # и считает первую «)» концом правила, так что путь со скобками развалил бы правило.
    with open(perms, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"allow": [bash_rule(reply + " *"),
                                             bash_rule('python3 "$RICK_HOME/tools/rick_bus.py" tell *')]}},
                  fh, ensure_ascii=False, indent=2)
    with open(launch, "w", encoding="utf-8") as fh:   # метки внешнего Claude снимаем: вложенный — свой
        fh.write("#!/bin/sh\nfor v in $(env | sed -En 's/^(CLAUDECODE|CLAUDE_CODE_[A-Za-z0-9_]*)=.*/\\1/p'); "
                 "do unset \"$v\"; done\ncd -- %s || exit 1\nexec %s -- \"$(cat %s)\"\n"
                 % (shlex.quote(folder), " ".join(map(shlex.quote, cmd + ["--settings", perms])),
                    shlex.quote(task_file)))
    os.chmod(launch, 0o700)
    tmux("new-session", "-d", "-s", sess, "-x", "220", "-y", "50", "-c", folder,
         "-e", "RICK_HOME=" + home, "/bin/sh " + shlex.quote(launch))
    wait(3)
    try:   # claude сразу вышел (чаще всего — не вошёл в аккаунт): сессия исчезла вместе с ним
        if not alive(sess):
            raise Gone()
        note = dialogs(sess, cfg)
    except Gone:
        raise TermError(UP_GONE) from None
    try:
        note = ", ".join(filter(None, [note, remember(name, task.split("\n", 1)[1] if "\n" in task else "")]))
    except (OSError, ValueError) as e:
        note = ", ".join(filter(None, [note, "память не записана: %s" % e]))
    return print("поднял %s в %s%s" % (sess, folder, "; " + note if note else "")) or 0


def remember(name, brief):
    """Чат поднят — строка «- <Проект> — <задача> (#N)» в «## Проекты» memory/NOW.md и досье
    memory/projects/<Проект>.md, если его нет: правило «запиши в том же ходе» модель исполняла через раз."""
    import tasks
    home = rick_config.home()
    open_task = tasks.listing(name, only_open=True, limit=1)
    what = ("%s (#%d)" % (open_task[0]["title"], open_task[0]["id"]) if open_task
            else " ".join(brief.split())[:80] or "чат поднят")
    path = os.path.join(home, "memory", "NOW.md")
    try:
        text = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        text = "# Сейчас\n\n## Проекты\n- (пусто)\n\n## Ждёт хозяина\n- (пусто)\n"
    lines, row = text.splitlines(), "- %s — %s" % (name, what)
    head = next((i for i, ln in enumerate(lines) if ln.startswith("## Проекты")), None)
    if head is None:
        lines += ["", "## Проекты", row]
    else:
        end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        mine = [i for prefix in ("- %s — " % name, "- (пусто") for i in range(head + 1, end)
                if nfc(lines[i]).startswith(prefix)]
        if mine:
            lines[mine[0]] = row
        else:
            last = max([i for i in range(head + 1, end) if lines[i].startswith("- ")], default=head)
            lines.insert(last + 1, row)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    dossier = os.path.join(home, "memory", "projects", name + ".md")
    if not os.path.exists(dossier):
        os.makedirs(os.path.dirname(dossier), exist_ok=True)
        with open(dossier, "x", encoding="utf-8") as fh:
            fh.write("# %s\n\n## Сейчас\n- %s\n\n## Ждёт хозяина\n- (пусто)\n\n## Хроника\n- %s · чат проекта "
                     "поднят: %s\n" % (name, what, time.strftime("%d.%m %H:%M"), what))
    return "в памяти: строка проекта и досье"


def choice(pane, label):
    return (re.search(r"(\d)\.\s*" + label, pane) or [None, None])[1]


def dialogs(sess, cfg):
    """Диалоги первого запуска на видимом экране. Возвращает пометку; неразобранный — TermError.

    Доверие к папке внутри projects_root — сами, если в ней нет CLAUDE_FILES (иначе запустились бы хуки, MCP и
    скиллы с allowed-tools клонированного репозитория); остальное — только при term_confirm_dialogs, bypass и auto —
    ещё и при таком же permission_mode в config.json."""
    notes, folder = [], sessions().get(sess, "")
    mine = bool(folder) and inside(folder, cfg["projects_root"])
    risky = [f for f in CLAUDE_FILES if folder and os.path.lexists(os.path.join(folder, f))]
    for _ in range(3):
        pane = capture(sess)
        kind, rx, yes, mode = next((d for d in DIALOGS if d[1].search(pane)), (None,) * 4)
        if not kind:
            if ASK_RX.search(tail := "\n".join(pane.splitlines()[-15:])) and choice(tail, "Yes"):
                raise TermError("в %s ждёт вопрос «Do you want…» — посмотри term.py read и ответь сам" % sess)
            return ", ".join(notes)
        if not cfg.get("term_confirm_dialogs") and not (kind == TRUST and mine and not risky):
            why = " (в папке свои настройки Claude: %s)" % ", ".join(risky) if kind == TRUST and risky else ""
            raise TermError("в %s диалог «%s»%s — подтверди сам (tmux attach -t %s) или включи "
                            "term_confirm_dialogs в config.json" % (sess, kind, why, exact(sess)))
        if mode and cfg.get("permission_mode") != mode:
            raise TermError("в %s просят %s, а в config.json он не задан — не подтверждаю" % (sess, kind))
        if not (digit := choice(pane, yes)):
            raise TermError("в %s диалог «%s», но пункта «%s» не вижу — ответь сам" % (sess, kind, yes))
        keys(sess, digit)
        wait()
        if rx.search(capture(sess)):
            keys(sess, "Enter")
            wait()
        notes.append("подтвердил: " + kind)
    raise TermError("в %s диалог не уходит — посмотри term.py read" % sess)


def delivered(sess, probe):
    """«дошло» — текст в ленте или в очереди; «в поле» — висит в поле ввода (между линейками ───)."""
    lines = capture(sess, 80).splitlines()
    rules = [i for i, line in enumerate(lines) if RULE_RX.match(line)]
    box, above = (lines[rules[-2] + 1:rules[-1]], lines[:rules[-2]]) if len(rules) > 1 else ([], lines)
    if any(probe in x or "Pasted text" in x for x in box):
        return "в поле"
    return "дошло" if re.search(r"queued", "\n".join(lines), re.I) or any(probe in x for x in above) else "не видно"


def cmd_send(args, cfg):
    sess = live(args.project, cfg)
    text = (sys.stdin.read() if args.text == "-" else args.text or "").replace("\r\n", "\n").replace("\r", "\n")
    # управляющие знаки вон: ни Esc, ни Ctrl+C из текста в окно не пройдут
    if not (text := "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32 and ord(ch) != 127).strip()):
        raise TermError("текст пустой — ничего не отправил")
    dialogs(sess, cfg)
    multi = "\n" in text or "\t" in text   # многострочное — вставкой, иначе каждая строка уйдёт Enter'ом
    keys(sess, "-l", "--", "\x1b[200~" + text + "\x1b[201~" if multi else text)
    wait(0.5)
    keys(sess, "Enter")
    probe = text.splitlines()[0][:40].strip()
    for _ in range(3):   # занятый чат рисует ленту не сразу: не одна пауза, а до трёх проверок
        wait()
        if (state := delivered(sess, probe)) != "не видно":
            break
    if state == "в поле":
        keys(sess, "Enter")
        wait()
        state = delivered(sess, probe)
    ok = state == "дошло"
    print("дошло: %s" % sess if ok else "не уверен, что дошло (%s) — проверь: term.py read %s" % (state, args.project))
    return 0 if ok else 1


def cmd_read(args, cfg):
    return print(capture(live(args.project, cfg), args.n)) or 0


def cmd_stop(args, cfg):
    tmux("kill-session", "-t", "=" + (sess := live(args.project, cfg)))
    return print("погасил %s" % sess) or 0


def context(folder):
    """(токены контекста, минут простоя) по последнему jsonl Claude Code этой папки.

    Claude Code кодирует путь, меняя всё, кроме латиницы и цифр, на «-»: кириллические папки одной длины
    попадают в одну папку — свой jsonl отбираем по полю cwd в первых строках."""
    root = os.environ.get("RICK_CLAUDE_PROJECTS") or os.path.expanduser("~/.claude/projects")
    files = sorted(glob.glob(os.path.join(glob.escape(root), re.sub(r"[^A-Za-z0-9]", "-", folder), "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    newest = next((f for f in files if jsonl_cwd(f) in (None, os.path.realpath(folder))), None)
    if not newest:
        return None, None
    idle = max(0, int((time.time() - os.path.getmtime(newest)) / 60))
    with open(newest, "rb") as fh:
        fh.seek(max(0, os.path.getsize(newest) - 3_000_000))
        lines = fh.read().decode("utf-8", "ignore").splitlines()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            usage = rec["message"]["usage"] if rec.get("type") == "assistant" and not rec.get("isSidechain") else None
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(usage, dict):
            return sum(int(usage.get(k) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")), idle
    return None, idle


def jsonl_cwd(path):
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for _, line in zip(range(20), fh):
            try:
                if cwd := json.loads(line).get("cwd"):
                    return os.path.realpath(cwd)
            except (ValueError, AttributeError):
                continue
    return None


def question(sess):
    """Хвост экрана (до 15 строк), если чат стоит на диалоге или вопросе «Do you want…»; иначе None."""
    try:
        tail = "\n".join(capture(sess).splitlines()[-15:])
    except TermError:   # чат погас между list-sessions и capture
        return None
    return tail if any(rx.search(tail) for rx in (TRUST_RX, BYPASS_RX, AUTO_RX, ASK_RX)) else None


def cmd_list(args, cfg):
    rows = [(n, f) for n, f in sessions().items() if n.startswith(PREFIX)]
    for name, folder in rows:
        ctx, idle = context(folder)
        flag = "  ⚠ >300K — новый чат с передачей" if ctx and ctx > CTX_LIMIT else ""
        flag += "  ⚠ ждёт ответа — term.py read" if question(name) else ""
        print("%s  %sK  простой %s мин  %s%s" % (name, ctx // 1000 if ctx is not None else "?",
                                                  "?" if idle is None else idle, folder, flag))
    return print("терминальных чатов нет") or 0 if not rows else 0


def cmd_watch(args, cfg):
    """Раз в 30 с — экраны всех rick-*; новый вопрос — печать и выход 0, нет до --max минут — выход 3.
    Уже показанный вопрос (тот же хвост экрана) второй раз не будит: state/watch-seen.json."""
    seen_file, deadline = os.path.join(rick_config.home(), "state", "watch-seen.json"), time.time() + args.max * 60
    while True:
        try:
            with open(seen_file, encoding="utf-8") as fh:
                seen = json.load(fh)
        except (OSError, ValueError):
            seen = {}
        now = {s: question(s) for s in sessions() if s.startswith(PREFIX)}
        new = {s: hashlib.sha1(t.encode("utf-8")).hexdigest() for s, t in now.items() if t}
        os.makedirs(os.path.dirname(seen_file), exist_ok=True)
        with open(seen_file, "w", encoding="utf-8") as fh:
            json.dump(new, fh)
        fresh = [s for s in sorted(new) if seen.get(s) != new[s]]
        if fresh:
            return print("\n".join("ВОПРОС в %s:\n%s" % (s, "\n".join(now[s].splitlines()[-8:])) for s in fresh)) or 0
        if time.time() >= deadline:
            return print("вопросов нет за %g мин" % args.max) or 3
        wait(20)   # 30 с при RICK_TERM_WAIT=1.5


def cmd_projects(args, cfg):
    if not os.path.isdir(root := cfg["projects_root"]):
        raise TermError("папки проектов %s нет — задай projects_root в config.json" % root)
    return print("\n".join(d for d in sorted(os.listdir(root)) if d[0] != "." and os.path.isdir(
        os.path.join(root, d)))) or 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="term.py", description="Терминальные чаты проектов в tmux rick-<Проект>.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd, opts in (("up", ["project", "--task", "--effort"]), ("send", ["project", "text"]),
                      ("read", ["project", "-n"]), ("stop", ["project"]), ("list", []), ("projects", []),
                      ("watch", ["--max"])):
        p = sub.add_parser(cmd)
        for o in opts:
            p.add_argument(o, **{"-n": {"type": int, "default": 60}, "--max": {"type": float, "default": 45}}.get(o, {}))
    args = ap.parse_args(argv)
    try:
        return globals()["cmd_" + args.cmd](args, rick_config.load())
    except (TermError, OSError) as e:
        return print("term.py: %s" % e, file=sys.stderr) or 2


if __name__ == "__main__":
    sys.exit(main())
