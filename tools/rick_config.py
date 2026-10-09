"""Настройки Рика: config.json в корне папки Рика (его создаёт install.sh из config.example.json).

  from rick_config import load, home
  cfg = load()          # словарь с умолчаниями, даже если config.json нет
  home()                # корень Рика: $RICK_HOME или папка над tools/

  python3 tools/rick_config.py status   — что подключено (Telegram, озвучка, разговор голосом, Напоминания, почта),
                                          словами; значения ключей не выводит, только «есть / нет».
"""
import json
import os
import shutil
import socket
import sys

DEFAULTS = {
    "owner": "босс",              # как Рик зовёт хозяина в ответах и журнале
    "projects_root": "~/Projects",  # где лежат папки проектов для term.py up
    "model": "",                  # модель терминальных чатов; пусто — по умолчанию Claude Code
    "effort": "high",             # эффорт терминальных чатов
    "permission_mode": "default",  # режим терминальных чатов: default | acceptEdits | auto | bypassPermissions
    "term_confirm_dialogs": False,  # term.py сам подтверждает доверие к чужой папке и согласие на bypass и auto
    "wake_on_mail": False,        # будить Рика ответами терминалов сразу (каждое пробуждение — платный ход)
    # notify.py — баннер и звук Мака; long_turn — звук после хода дольше минуты, баннер — если ответ ждёт хозяина
    "notify": {"sound": True, "banner": True, "long_turn": True},
    "voice": {"engine": "say", "say_voice": "Milena", "say_rate": "175"},
}


def home():
    env = os.environ.get("RICK_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(os.path.join(home(), "config.json"), encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    cfg["projects_root"] = os.path.expanduser(cfg["projects_root"])
    return cfg


def _env_keys(path):
    """Имена ключей с непустым значением в .env — сами значения не читаются наружу."""
    keys = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                name, eq, value = line.strip().partition("=")
                if eq and not name.startswith("#") and value.strip().strip("'\"").strip():
                    keys.add(name.strip().replace("export ", ""))
    except OSError:
        pass
    return keys


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except (OSError, ValueError):
        return False


def status():
    """Строки «что — состояние» словами владельца. Перед любым «не подключено» Рик смотрит сюда."""
    root, cfg = home(), load()
    has = lambda name: bool(shutil.which(name) or any(  # noqa: E731 — у ssh и launchd PATH без Homebrew
        os.access(os.path.join(d, name), os.X_OK) for d in ("/opt/homebrew/bin", "/usr/local/bin")))
    env = set(k for k in os.environ if os.environ.get(k)) | _env_keys(os.path.join(root, ".env"))
    kit = os.path.join(root, "telethon_kit")
    kit_env = _env_keys(os.path.join(kit, ".env"))
    voice_env = set(k for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)) | \
        _env_keys(os.path.join(root, "voice", ".env"))
    pr = cfg["projects_root"]
    n = len([e for e in os.listdir(pr) if not e.startswith(".") and os.path.isdir(os.path.join(pr, e))]) \
        if os.path.isdir(pr) else 0
    out = ["Хозяин: %s%s" % (cfg["owner"], "" if os.path.exists(os.path.join(root, "config.json"))
                             else " (настроек нет — по умолчанию)"),
           "Папка проектов: %s" % ("%s, папок %d" % (pr, n) if os.path.isdir(pr) else "%s — её нет" % pr),
           "Чаты проектов: %s" % ("есть (tmux)" if has("tmux") else "нет tmux — ставит ./install.sh")]
    bot = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} <= env
    out.append("Telegram-бот (файлы хозяину): %s" % ("подключён" if bot else "не подключён — ключи в .env"))
    venv = os.path.isdir(os.path.join(kit, ".venv"))
    keys = {"TELEGRAM_API_ID", "TELEGRAM_API_HASH"} <= kit_env
    on = "TELEGRAM_USERBOT_ENABLED" in kit_env
    login = os.path.exists(os.path.join(kit, "user_session.session"))
    if venv and keys and on and login:
        tg = "подключён"
    else:
        miss = [w for w, ok in (("установка ./install.sh --telegram", venv), ("ключи", keys),
                                ("включение в telethon_kit/.env", on), ("вход", login)) if not ok]
        tg = "не подключён — нет: " + ", ".join(miss)
    out.append("Telegram от твоего аккаунта: " + tg)
    eng = (cfg.get("voice") or {}).get("engine") or "say"
    if eng == "elevenlabs":
        voice = "ElevenLabs" if "ELEVENLABS_API_KEY" in voice_env else "ElevenLabs без ключа — говорит голос Мака"
    else:
        voice = "голос Мака" if has("say") else "нет (нет say)"
    out.append("Озвучка ответов: " + voice)
    port = os.environ.get("LIVE_PORT") or "8787"
    out.append("Разговор голосом (страница live): %s" % (
        "страница поднята" if _port_open(port) else
        ("не поднята, ключ OpenAI есть" if "OPENAI_API_KEY" in voice_env else "не подключён — нет ключа OpenAI")))
    out.append("Напоминания: %s" % ("через «Напоминания» Мака (первый раз Мак спросит доступ)" if has("osascript")
                                    else "нет — это не Мак"))
    out.append("Ответы чатов проектов: %s" % (
        "будят Рика сразу (wake_on_mail)" if cfg.get("wake_on_mail")
        else "приходят с твоим следующим словом (wake_on_mail выключен)"))
    out.append("Режим чатов проектов: %s" % {"bypassPermissions": "без подтверждений", "acceptEdits":
                                            "правки файлов без спроса", "auto": "Claude сам проверяет каждое действие"
                                            }.get(cfg.get("permission_mode"), "спрашивают разрешения"))
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["status"]:
        print("\n".join(status()))
        sys.exit(0)
    sys.exit("Использование: python3 tools/rick_config.py status")
