#!/bin/bash
# Установка Рика: ./install.sh [--owner "Имя"] [--telegram]
# Без sudo, никуда не входит, никому ничего не шлёт. Повторный запуск безопасен.

cd "$(dirname "$0")" || exit 1
RICK="$(pwd -P)"
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
MIN_CLAUDE="2.1.141"   # проверено на этой версии: хуки с asyncRewake

OWNER=""
TELEGRAM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --owner)   # без значения shift 2 не сдвинул бы ничего — цикл навсегда
      if [ $# -lt 2 ] || [ -z "$2" ] || [ "${2#--}" != "$2" ]; then
        echo "--owner без имени: ./install.sh --owner \"Имя\""; exit 1
      fi
      OWNER="$2"; shift 2 ;;
    --owner=*) OWNER="${1#--owner=}"; shift ;;
    --telegram) TELEGRAM=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Не знаю параметр: $1"; exit 1 ;;
  esac
done

OK=()
MISS=()
ok()   { OK+=("$1"); }
miss() { MISS+=("$1"); }

echo
echo "=== Установка Рика ==="
echo

# 1. Только macOS
if [ "$(uname -s)" != "Darwin" ]; then
  echo "Рик работает только на macOS (tmux, say, afplay)."
  exit 1
fi

# 2. python3 >= 3.9 — без него Рик не работает
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "Нужен python3 версии 3.9 или новее."
  echo "Поставить: xcode-select --install — и запусти установку ещё раз."
  exit 1
fi
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"

# 3. tmux — для чатов проектов в терминале
if command -v tmux >/dev/null 2>&1; then
  ok "tmux на месте"
else
  miss "tmux — нужен для чатов проектов. Поставить: brew install tmux"
fi

# 4. Claude Code
if command -v claude >/dev/null 2>&1; then
  CV="$(claude --version 2>/dev/null | awk '{print $1}')"
  if python3 - "$CV" "$MIN_CLAUDE" <<'PYEOF'
import sys
v = lambda s: tuple(int(x) for x in s.split(".") if x.isdigit())
sys.exit(0 if v(sys.argv[1]) >= v(sys.argv[2]) else 1)
PYEOF
  then
    ok "Claude Code $CV"
  else
    miss "Claude Code $CV — старый, нужен $MIN_CLAUDE или новее: claude update"
  fi
else
  miss "Claude Code — не найден. Поставить: https://claude.com/claude-code"
fi

# 5. Настройки: config.json из примера, имя хозяина, режим чатов проектов, пробуждение почтой
[ -f config.json ] || cp config.example.json config.json
setcfg() {  # setcfg <ключ> <значение JSON>
  python3 - "$1" "$2" <<'PYEOF'
import json, sys
with open("config.json", encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg[sys.argv[1]] = json.loads(sys.argv[2])
with open("config.json", "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PYEOF
}
getcfg() {  # getcfg <ключ> → значение из config.json (JSON true/false — как true/false), пусто — нет ключа
  python3 - "$1" <<'PYEOF' 2>/dev/null
import json, sys
v = json.load(open("config.json", encoding="utf-8")).get(sys.argv[1])
print("" if v is None else json.dumps(v) if isinstance(v, bool) else v)
PYEOF
}
jstr() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1].strip()))' "$1"; }
rootval() { case "$1" in "~"*) jstr "$1" ;; *) jstr "$(fullpath "$1")" ;; esac; }   # относительный путь — от сюда
fullpath() { python3 -c 'import os, sys; print(os.path.abspath(os.path.expanduser(sys.argv[1].strip())))' "$1"; }
yesno() {  # ответ → y | n | пусто (Enter) | ? — словом целиком; bash 3.2 сравнивает байты и регистр кириллицы не знает
  python3 - "$1" <<'PYEOF'
import sys
a = sys.argv[1].strip().lower()
print("" if not a else "y" if a in ("y", "yes", "д", "да") else "n" if a in ("n", "no", "н", "нет") else "?")
PYEOF
}
# Умолчание каждого вопроса — то, что уже в config.json; пустой ответ (Enter) ничего не пишет.
if [ -z "$OWNER" ] && [ -t 0 ]; then
  printf "Как Рику тебя звать? [%s]: " "$(getcfg owner)"
  read -r OWNER
fi
[ -n "$OWNER" ] && setcfg owner "$(jstr "$OWNER")"
if [ -t 0 ]; then
  printf "Где лежат твои проекты? [%s]: " "$(getcfg projects_root)"
  read -r ROOT
  [ -n "$ROOT" ] && setcfg projects_root "$(rootval "$ROOT")"
  for _ in 1 2 3 4 5; do
    ROOTDIR="$(fullpath "$(getcfg projects_root)")"
    [ -d "$ROOTDIR" ] && break
    printf "Папки %s нет. Создать — Enter или «да»; другая папка — впиши путь; «нет» — оставить: " "${ROOTDIR}"
    read -r ANS || break
    case "$(yesno "$ANS")" in
      ""|y) mkdir -p "$ROOTDIR" && echo "  создал ${ROOTDIR}" ;;
      n) break ;;
      *) setcfg projects_root "$(rootval "$ANS")" ;;
    esac
  done
  # режим auto — только если его знает этот Claude Code (зависит от версии и тарифа)
  AUTO=0; claude --help 2>/dev/null | grep -q '"auto"' && AUTO=1
  case "$(getcfg permission_mode)" in bypassPermissions) CURMODE=2 ;; auto) CURMODE=3 ;; *) CURMODE=1 ;; esac
  # в config auto, а этот Claude Code его не знает — умолчание 1: Enter не оставит режим, которого нет
  NOAUTO_CFG=0; [ "$CURMODE" = 3 ] && [ "$AUTO" = 0 ] && { CURMODE=1; NOAUTO_CFG=1; }
  echo "Чаты проектов:"
  echo "  1) спрашивают разрешения, как обычный Claude Code"
  echo "  2) без вопросов — делают всё сами; быстрее, но на твой риск"
  [ "$AUTO" = 1 ] && echo "  3) auto — Claude сам проверяет каждое действие, рискованное блокирует; рекомендую"
  [ "$NOAUTO_CFG" = 1 ] && echo "  (в настройках стоит auto, но этот Claude Code его не знает)"
  if [ "$AUTO" = 1 ]; then CHOICES="1, 2 или 3"; else CHOICES="1 или 2"; fi
  if [ "$NOAUTO_CFG" = 1 ]; then printf "Выбери %s [1]: " "$CHOICES"; else printf "Выбери %s [%s — сейчас]: " "$CHOICES" "$CURMODE"; fi
  read -r MODE
  [ -z "$MODE" ] && [ "$NOAUTO_CFG" = 1 ] && MODE=1
  case "$MODE" in
    "") ;;
    2) setcfg permission_mode '"bypassPermissions"'; setcfg term_confirm_dialogs true ;;
    1) setcfg permission_mode '"default"'; setcfg term_confirm_dialogs false ;;
    3) if [ "$AUTO" = 1 ]; then setcfg permission_mode '"auto"'; setcfg term_confirm_dialogs true
       else echo "  режима auto в этом Claude Code нет — оставляю как было"; fi ;;
    *) echo "  не понял «${MODE}» — оставляю как было" ;;
  esac
  if [ "$(getcfg wake_on_mail)" = true ]; then CURWAKE="Y/n"; else CURWAKE="y/N"; fi
  printf "Будить Рика ответами чатов, пока ты молчишь (каждое пробуждение — платный ход)? [%s]: " "$CURWAKE"
  read -r WAKE
  case "$(yesno "$WAKE")" in
    "") ;;
    y) setcfg wake_on_mail true ;;
    n) setcfg wake_on_mail false ;;
    *) echo "  не понял «${WAKE}» — оставляю как было" ;;
  esac
fi
ok "Настройки — config.json (хозяин: $(python3 -c 'import json; c = json.load(open("config.json")); print(c.get("owner", ""), "· чаты:", c.get("permission_mode", "default"), "· будить почтой:", c.get("wake_on_mail", False))'))"

ROOTDIR="$(fullpath "$(getcfg projects_root)")"
# Папку проектов создаём только с согласия в терминале; без терминала — лишь строка «не хватает»
if [ -d "$ROOTDIR" ]; then
  ok "Папка проектов — ${ROOTDIR}"
else
  miss "Папки проектов ${ROOTDIR} нет — создай её или впиши свою в projects_root (config.json)"
fi

# 6. Папки памяти и рабочего состояния; рабочая память — из шаблонов, если её ещё нет (в git только шаблоны)
mkdir -p state inbox journal archive memory/projects live/log voice/cache
for f in NOW people; do
  [ -f "memory/$f.md" ] || cp "memory/$f.example.md" "memory/$f.md"
done
chmod +x tools/*.py live/start.sh voice/play.sh 2>/dev/null

# 7. Память и почта отвечают
if [ -f tools/rick_bus.py ] && python3 tools/rick_bus.py state >/dev/null 2>&1 \
   && echo '{}' | python3 tools/mem_hook.py start >/dev/null 2>&1; then
  ok "Память и почта работают"
else
  miss "Память и почта не запустились — открой Claude в этой папке и покажи ему вывод"
fi

# 8. Голос Мака
if say -v '?' 2>/dev/null | grep -q '^Milena'; then
  ok "Русский голос Мака (Milena) на месте"
else
  echo "  (по желанию) русский голос Milena: Системные настройки → Универсальный доступ → Устный контент → Управлять голосами"
fi

# 9. Telegram — только по флагу --telegram
if [ "$TELEGRAM" = 1 ]; then
  VENV=telethon_kit/.venv
  echo "Готовлю Telegram-комплект (скачиваю библиотеки)…"
  python3 -m venv "$VENV" >/dev/null 2>&1 \
    && "$VENV/bin/python" -m pip install -q --disable-pip-version-check -r telethon_kit/requirements.txt >/dev/null 2>&1
  if "$VENV/bin/python" -c 'import telethon' >/dev/null 2>&1; then
    ok "Telegram-комплект собран — ключи и вход: docs/telegram.md"
  else
    miss "Telegram-комплект не собрался — нужен интернет, запусти ./install.sh --telegram ещё раз"
  fi
fi

echo
echo "ГОТОВО:"
for s in "${OK[@]}"; do echo "  + $s"; done
if [ ${#MISS[@]} -gt 0 ]; then
  echo
  echo "НЕ ХВАТАЕТ:"
  for s in "${MISS[@]}"; do echo "  - $s"; done
fi
echo
echo "Дальше: claude в папке $RICK — и скажи «привет, Рик»."
echo
[ ${#MISS[@]} -eq 0 ]
