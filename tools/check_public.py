#!/usr/bin/env python3
"""Проверка перед публикацией: в папке не должно быть ключей, телефонов, почты, денег, чужих путей и личных имён.

  python3 tools/check_public.py [папка] [--deny стоп-лист.txt]

Что смотрит: папка — git-репозиторий — то, что уйдёт в git: `git ls-files` и неотслеживаемое неигнорируемое
(`git ls-files -o --exclude-standard`); игнорируемое (память, ключи, state/) не трогает. Не корень своего git (в т.ч. папка внутри чужого репозитория) — обходит всё.
Печатает «Проверено файлов: N»; N = 0 — выход 1: «ничего не смотрел» не равно «чисто».
Рабочие папки (state/, inbox/ …), `.env` и `*.session` среди этих файлов — находка сами по себе.
Стоп-лист — по слову в строке (имена людей, названия мест, ники, личные папки); держи его ВНЕ папки, которую
публикуешь. Строку, где совпадение законно, помечай в конце словом `public-ok`. Выход 1 — есть находки.
"""
import os
import re
import subprocess
import sys

PATTERNS = {
    "ключ": re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|sk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{20,}"
                       r"|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"),
    "токен бота": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "секрет в присвоении": re.compile(r"(?i)\b(api[_-]?(key|hash|secret)|token|password|passwd|secret)\b\s*[=:]\s*"
                                      r"['\"]?[A-Za-z0-9_\-/+=]{12,}"),
    "приватный ключ": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "телефон": re.compile(r"(?<![\w.])\+\d{1,3}[\s(-]*\d{2,4}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"
                          r"|(?<![\w.])8[\s(-]*[79]\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"),
    "почта": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "чужой путь": re.compile(r"/Users/(?!\$|<|you\b|user\b|name\b)[A-Za-z][\w.-]*|/home/(?!user\b)[a-z][\w.-]*"),
    "id чата": re.compile(r"(?<![\w.])-100\d{9,}\b"),
    "деньги": re.compile(r"\d[\d\s.,]*\s?(₸|тенге|тг\b|руб|₽|€)|\$\s?\d{2,}|\$\s?\d+[.,]\d"),  # $1 — не деньги
    "папка чата Claude": re.compile(r"-Users-[A-Za-z][\w.]*-"),
}
# Номер и ключ, разорванные переносом строки («+7 900 "\n"000-00-00"»): ищем ещё в склейке соседних строк.
JOINED = ("ключ", "токен бота", "телефон")
TAIL = re.compile(r"""["']?\s*\+?\s*\\?\s*$""")      # конец строки: кавычка, «+» склейки, «\» переноса
HEAD = re.compile(r"""^\s*(?:\+\s*(?=["']))?["']?""")   # начало следующей: отступ, «+ "» склейки, кавычка
FORBIDDEN_DIRS = {"state", "inbox", "dossiers", "board-archive"}
ALLOWED_EMAILS = re.compile(r"noreply@|example\.(com|org)|@users\.noreply\.github\.com")
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
BINARY = (".png", ".jpg", ".jpeg", ".gif", ".mp3", ".wav", ".m4a", ".ico", ".pdf", ".zip", ".session")


class Deny:
    """Слово стоп-листа: отдельным словом; частью слитного имени (CamelCase, «_», «-»: «ИмяФамилия»);
    от 5 букв — ещё и подстрокой внутри слова («AcmeShopKz», «ivanovpetr»)."""
    PART = re.compile(r"[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])")

    def __init__(self, word):
        if re.search(r"[а-яё]", word, re.I) and len(word) >= 5:   # имя в любом падеже: основа + до трёх букв
            stem = re.sub(r"[аеёиоуыэюяьй]$", "", word, flags=re.I)
            self.whole = re.compile(r"(?<![\wа-яё])" + re.escape(stem) + r"[а-яё]{0,3}(?![\wа-яё])", re.I)
        else:
            self.whole = re.compile(r"(?<![\wа-яё])" + re.escape(word) + r"(?![\wа-яё])", re.I)
        self.inner = re.compile(re.escape(word), re.I) if len(word) >= 5 else None

    def search(self, line):
        m = self.whole.search(line)
        if m:
            return m
        for token in re.finditer(r"[\wа-яё]+", line):
            for part in self.PART.finditer(token.group(0)):
                m = self.whole.fullmatch(part.group(0))
                if m:
                    return m
            m = self.inner.search(token.group(0)) if self.inner else None
            if m:
                return m
        return None


def load_deny(path):
    words = []
    if path:
        with open(path, encoding="utf-8") as fh:
            words = [w.strip() for w in fh if w.strip() and not w.startswith("#")]
    return [Deny(w) for w in words]


def git_files(root):
    """Файлы, которые уйдут в git (отслеживаемые + неотслеживаемые неигнорируемые), или None — не свой git.

    Свой — только если папка и есть корень репозитория: внутри чужого репозитория, который её игнорирует,
    ls-files вернул бы пусто, и «0 находок» значило бы «ничего не смотрел»."""
    try:
        top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except OSError:
        return None
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(root):
        return None
    out = []
    for extra in ([], ["-o", "--exclude-standard"]):
        try:
            done = subprocess.run(["git", "-C", root, "ls-files", "-z", *extra], capture_output=True)
        except OSError:
            return None
        if done.returncode != 0:
            return None
        out += [f for f in done.stdout.decode("utf-8", "surrogateescape").split("\0") if f]
    return sorted(set(out))


def files_of(root):
    """[(путь, путь от корня)] к проверке и корень."""
    if os.path.isfile(root):
        return [(root, os.path.basename(root))], os.path.dirname(root) or "."
    listed = git_files(root)
    if listed is None:
        listed = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            listed += [os.path.relpath(os.path.join(d, f), root) for f in files]
    return [(os.path.join(root, rel), rel) for rel in listed if os.path.isfile(os.path.join(root, rel))], root


def scan(root, deny, listed=None):
    found, seen_dirs = [], set()
    paths, root = listed or files_of(root)
    for path, rel in paths:
        f = os.path.basename(rel)
        for i, part in enumerate(rel.split(os.sep)[:-1]):
            bad = os.sep.join(rel.split(os.sep)[:i + 1])
            if part in FORBIDDEN_DIRS and bad not in seen_dirs:
                seen_dirs.add(bad)
                found.append((bad, 0, "рабочая папка в выгрузке", part))
        if f.endswith(BINARY):
            found.append((rel, 0, "бинарный файл", f)) if f.endswith(".session") else None
            continue
        if f == ".env" or f.startswith(".env.") and f != ".env.example" or f.endswith(".session-journal"):
            found.append((rel, 0, "файл ключей", f))
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            if "public-ok" in line:
                continue
            for kind, rx in PATTERNS.items():
                for m in rx.finditer(line):
                    s = m.group(0)
                    if kind == "почта" and ALLOWED_EMAILS.search(s):
                        continue
                    if kind == "IP" and s in ALLOWED_IPS:
                        continue
                    found.append((rel, n, kind, s))
            for rx in deny:
                m = rx.search(line)
                if m:
                    found.append((rel, n, "стоп-лист", m.group(0)))
        for n, (a, b) in enumerate(zip(lines, lines[1:]), 1):
            if "public-ok" in a or "public-ok" in b:
                continue
            a, b = TAIL.sub("", a), HEAD.sub("", b)
            for kind in JOINED:
                for m in PATTERNS[kind].finditer(a + b):
                    if m.start() < len(a) < m.end():                 # только то, что на стыке
                        found.append((rel, n, kind + " через перенос", m.group(0)))
    return found


def main(argv):
    args, deny_path = list(argv), None
    if "--deny" in args:
        i = args.index("--deny")
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            print("--deny без файла: check_public.py [папка] --deny <стоп-лист>", file=sys.stderr)
            return 2
        deny_path = args.pop(i + 1)
        args.pop(i)
    args = [a for a in args if not a.startswith("--")]
    root = args[0] if args else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    listed = files_of(root)
    found = scan(root, load_deny(deny_path), listed)
    for rel, n, kind, s in found:
        print("%s:%d  %s  %s" % (rel, n, kind, s[:60]))
    print("Проверено файлов: %d" % len(listed[0]))
    if not listed[0]:
        print("Ничего не проверено — не та папка или всё игнорируется; это не «чисто»")
        return 1
    print("Находок: %d" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
