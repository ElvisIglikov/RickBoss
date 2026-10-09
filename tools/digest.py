#!/usr/bin/env python3
"""«Что у нас» одним вызовом: сводка всех проектов по их status.md и живым чатам rick-*.

  digest.py                  — текстом: идут, ждут хозяина, тихо
  digest.py --json           — то же списком для разбора
  digest.py --html [--open]  — файл state/digest-ГГГГММДД-ЧЧММ.html: что решить, что идёт, подвисшие вопросы

Источник — docs/status.md проекта (запасной — status.md в корне; форма — docs/status-format.md) в папке
projects_root из config.json и живые tmux-сессии rick-* (term.sessions()). Обход чатов не нужен: дёшево по контексту.
Подмены для тестов: RICK_HOME, RICK_TMUX.
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402
import term  # noqa: E402

STALE_S = 24 * 3600
STATUS_FILES = ("docs/status.md", "status.md")
# Блок воркфлоу: «1️⃣ Workflow 💭 идёт», «1️⃣ … ✅» (номер-эмодзи) или старое «Workflow …» / «✅ Workflow …»
# (перед словом — только значок, без букв). Строки «Сейчас: …», «Итог: Workflow …», «Workflow: …» — не заголовок.
HEAD_RE = re.compile(r"^((?:\d️?⃣)+|(?:[^\w\s:]+\s*)?Workflow\b(?!\s*:))(.*)$")
WAIT_PREFIXES = ("Ждёт хозяина:", "Ждет хозяина:", "Ждёт пользователя:")
REC_RE = re.compile(r"рекоменд\w*[:\s—-]*(.+)$", re.I)
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z]+)\}\}")
COLORS = ("#f5a45d", "#7fc48f", "#7fb2e6", "#d98fd0", "#e6c35c", "#7fd1c4", "#e68f8f", "#a99be6")


def clip(text, limit=110):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def nothing_said(text):
    return text.strip().strip(".").lower() in ("", "ничего", "нет", "—", "-")


def parse(text):
    """Шапка, итоги и блоки воркфлоу одного status.md."""
    out = {"title": "", "summary": [], "workflows": [], "now": "", "wait": ""}
    block = None
    for raw in text.splitlines():
        line = raw.strip()
        if not out["title"] and line.startswith("# "):
            out["title"] = line[2:].strip()
            continue
        # Строки-ключи раньше заголовков: «Сейчас: Workflow 2 на проверке.» — не блок воркфлоу.
        if line.startswith("Сейчас:"):
            out["now"] = line[len("Сейчас:"):].strip()
            continue
        if line.startswith(WAIT_PREFIXES):
            out["wait"] = line.split(":", 1)[1].strip()
            continue
        head = HEAD_RE.match(line)
        if head and not line.startswith(("-", "*")):
            word = line.lower()
            state = ("идёт" if "идёт" in word or "💭" in line or "⏳" in line else
                     "готово" if "готов" in word or "✅" in line else
                     "упал" if "упал" in word or "стоп" in word or "❌" in line or "🛑" in line else
                     "запланирован" if "запланир" in word or "⬜" in line else "ждёт")
            block = {"state": state, "about": "", "steps": ""}
            out["workflows"].append(block)
            continue
        if block is None:
            if line and not line.startswith(("обновлено", "#")):
                out["summary"].append(line)
            continue
        item = line[2:].strip() if line.startswith(("- ", "* ")) else ""
        if item.startswith("о чём:"):
            block["about"] = item[len("о чём:"):].strip()
        elif re.match(r"шаги \d+ из \d+", item):
            block["steps"] = "/".join(re.findall(r"\d+", item)[:2])
    return out


def terminal_of(folder, sessions):
    """Живой чат этой папки: rick-* с той же папкой на диске или с именем папки."""
    for name, path in sessions.items():
        if name.startswith(term.PREFIX) and term.same(path, folder):
            return name
    name = term.session(os.path.basename(folder))
    return name if name in sessions else ""


def collect(root, sessions, now):
    items = []
    names = sorted(os.listdir(root), key=str.lower) if os.path.isdir(root) else []
    for base in names:
        folder = os.path.join(root, base)
        # Рабочее дерево git (.git — файл) — копия проекта для параллельного прогона, не проект.
        if not os.path.isdir(folder) or base.startswith(".") or os.path.isfile(os.path.join(folder, ".git")):
            continue
        status = next((os.path.join(folder, rel) for rel in STATUS_FILES
                       if os.path.isfile(os.path.join(folder, rel))), None)
        chat = terminal_of(folder, sessions)
        if status is None and not chat:
            continue
        try:
            with open(status, encoding="utf-8", errors="replace") as fh:
                parsed, mtime = parse(fh.read()), os.path.getmtime(status)
        except (OSError, TypeError):
            parsed, mtime = parse(""), None
        flows = parsed["workflows"]
        stale = mtime is not None and now - mtime > STALE_S
        running = [i for i, flow in enumerate(flows, 1) if flow["state"] == "идёт"]
        wait = "" if nothing_said(parsed["wait"]) else parsed["wait"]
        group = "идут" if running and not stale else "ждут" if wait else "тихо"
        items.append({"project": term.nfc(base), "title": parsed["title"] or term.nfc(base), "group": group,
                      "workflows": len(flows), "done": sum(flow["state"] == "готово" for flow in flows),
                      "running": [{"n": i, "about": flows[i - 1]["about"], "steps": flows[i - 1]["steps"]}
                                  for i in running],
                      "summary": parsed["summary"][:3], "now": parsed["now"], "wait": wait,
                      "stale": stale, "terminal": chat,
                      "updated": datetime.datetime.fromtimestamp(mtime).strftime("%d.%m %H:%M") if mtime else ""})
    return items


def text(items):
    if not items:
        return "Сводок проектов нет: ни status.md в папках проектов, ни живых чатов rick-*"
    lines = []
    for group, label in (("идут", "⏳ Идут"), ("ждут", "✋ Ждут хозяина"), ("тихо", "· Тихо")):
        part = [item for item in items if item["group"] == group]
        # вопрос идущего проекта — тоже «ждёт хозяина»: в счёт входит, сам вопрос — в «Идут»
        asked = [item for item in items if item["wait"] and item["group"] == "идут"] if group == "ждут" else []
        if not part and not asked:
            continue
        if group == "тихо":
            lines.append("%s: %s" % (label, ", ".join(item["title"] for item in part)))
            continue
        lines.append("%s — %d" % (label, len(part) + len(asked)))
        if asked:
            lines.append("  ещё в «Идут»: " + ", ".join(item["title"] for item in asked))
        for item in part:
            head = "  %s · Workflow %d из %d готово" % (item["title"], item["done"], item["workflows"])
            if item["terminal"]:
                head += " · чат " + item["terminal"]
            if item["stale"]:
                head += " · сводка старше суток"
            lines.append(head)
            for flow in item["running"]:
                steps = " (шаги %s)" % flow["steps"].replace("/", " из ") if flow["steps"] else ""
                lines.append("    %d-й идёт%s: %s" % (flow["n"], steps, clip(flow["about"], 90)))
            if group == "идут" and item["now"]:
                lines.append("    Сейчас: " + clip(item["now"]))
            if item["wait"]:
                lines.append("    Ждёт: " + clip(item["wait"]))
    return "\n".join(lines)


# ---- HTML ---------------------------------------------------------------------------------------------------
def color(name):
    return COLORS[int(hashlib.sha1(name.encode("utf-8")).hexdigest(), 16) % len(COLORS)]


def esc(value):
    return html.escape(str(value or ""), quote=True)


def question(n, item):
    c = color(item["project"])
    kick = item["title"] + (" · обновлено " + item["updated"] if item["updated"] else "")
    if item["stale"]:
        kick += " · сводка старше суток"
    rec = REC_RE.search(item["wait"])
    parts = ['<section class="pt" id="q%d" data-n="%d" data-project="%s" style="--c:%s">' % (n, n, esc(item["title"]), c),
             '<div class="hd"><span class="no">%d</span><span class="kick">%s</span></div>' % (n, esc(kick)),
             '<p class="gist">%s</p>' % esc(item["wait"])]
    if rec:
        parts.append('<p class="ans"><span class="rec"><i>рекомендую:</i> %s</span></p>' % esc(rec.group(1)))
    chips = "".join('<label class="chip"><input type="radio" name="a%d" value="%s"><span>%s</span></label>'
                    % (n, w, w) for w in ("да", "нет", "позже"))
    parts.append('<div class="pick" role="group" aria-label="Ответ на %d">%s</div>' % (n, chips))
    parts.append('<textarea class="own" rows="1" placeholder="свой ответ" aria-label="Свой ответ на %d"></textarea>' % n)
    parts.append("</section>")
    return "\n".join(parts)


def running_card(item):
    c = color(item["project"])
    head = "Workflow %d из %d готово" % (item["done"], item["workflows"])
    if item["terminal"]:
        head += " · чат " + item["terminal"]
    rows = ['<section class="pt run" style="--c:%s">' % c,
            '<div class="hd"><span class="pname">%s</span><span class="kick">%s</span></div>'
            % (esc(item["title"]), esc(head))]
    for flow in item["running"]:
        steps = " · шаги %s" % flow["steps"].replace("/", " из ") if flow["steps"] else ""
        rows.append('<p class="flow"><b>%d-й идёт</b>%s — %s</p>' % (flow["n"], esc(steps), esc(clip(flow["about"], 140))))
    if item["now"]:
        rows.append('<p class="note">Сейчас: %s</p>' % esc(item["now"]))
    rows.append("</section>")
    return "\n".join(rows)


def render(items, when, template):
    fresh = [i for i in items if i["wait"] and not i["stale"]]
    hung = [i for i in items if i["wait"] and i["stale"]]
    going = [i for i in items if i["group"] == "идут"]
    quiet = [i for i in items if i["group"] == "тихо" and not i["wait"]]
    count = {"идут": len(going), "ждут": len(fresh) + len(hung), "тихо": len(quiet)}   # шапка = то, что ниже
    toc = "".join('<li><a href="#q%d" style="--c:%s">%d</a></li>' % (n, color(i["project"]), n)
                  for n, i in enumerate(fresh + hung, 1))
    header = ['<span class="plate">СВОДКА · %s</span>' % esc(when.strftime("%d.%m.%Y %H:%M")),
              "<h1>Что у нас</h1>",
              '<p class="who">проектов %d · идут %d · ждут тебя %d · тихо %d</p>'
              % (len(items), count["идут"], count["ждут"], count["тихо"])]
    if fresh or hung:
        header.append('<p class="hint">Отметь ответ или впиши свой, потом «Скопировать ответы» и вставь Рику.</p>')
        header.append('<div class="tocg"><span class="tl">Вопросы</span><ul class="toc">%s</ul></div>' % toc)
    body = ['<h2 class="grp" id="g-wait">Реши</h2>']
    body += [question(n, i) for n, i in enumerate(fresh, 1)] or ['<p class="empty">Решать нечего.</p>']
    body.append('<h2 class="grp" id="g-run">Идёт<small>для сведения</small></h2>')
    body += [running_card(i) for i in going] or ['<p class="empty">Сейчас ничего не идёт.</p>']
    if quiet:
        body.append('<p class="quiet"><b>Тихо:</b> %s</p>' % esc(", ".join(i["title"] for i in quiet)))
    if hung:
        body.append('<h2 class="grp" id="g-hung">Подвисшие вопросы<small>сводка старше суток — '
                    'проверь, не решено ли уже</small></h2>')
        body += [question(n, i) for n, i in enumerate(hung, len(fresh) + 1)]
    if not items:
        body = ['<p class="empty">Сводок нет: ни status.md в папках проектов, ни живых чатов.</p>']
    marks = {"TITLE": esc("Сводка " + when.strftime("%d.%m %H:%M")), "HEADER": "\n".join(header),
             "BODY": "\n".join(body)}
    # Один проход: подставленный текст (status.md с «{{BODY}}» внутри) повторно не разбирается.
    return PLACEHOLDER_RE.sub(lambda m: marks.get(m.group(1), m.group(0)), template)


def write_html(items, home, now=None):
    when = now or datetime.datetime.now()
    with open(os.path.join(home, "templates", "digest.html"), encoding="utf-8") as fh:
        page = render(items, when, fh.read())
    os.makedirs(os.path.join(home, "state"), exist_ok=True)
    path = os.path.join(home, "state", "digest-%s.html" % when.strftime("%Y%m%d-%H%M"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


def sessions():
    try:
        return term.sessions()
    except (term.TermError, OSError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="digest.py", description="Сводка проектов по status.md и чатам rick-*.")
    ap.add_argument("--json", action="store_true", help="списком для разбора")
    ap.add_argument("--html", action="store_true", help="файл state/digest-ГГГГММДД-ЧЧММ.html")
    ap.add_argument("--open", action="store_true", help="открыть файл (с --html)")
    args = ap.parse_args(argv)
    cfg = rick_config.load()
    items = collect(cfg["projects_root"], sessions(), time.time())
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif not (args.html or args.open):
        print(text(items))
    if args.html or args.open:
        try:
            path = write_html(items, rick_config.home())
        except OSError as e:
            return print("digest.py: %s" % e, file=sys.stderr) or 2
        print(path)
        if args.open:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
