#!/usr/bin/env python3
"""Файл хозяину в его чат со своим Telegram-ботом (Bot API, не юзербот: писать может только хозяину).

  python3 tools/bot_send.py <файл> "#тема подпись"              — отправить файл с подписью (тихо)
  python3 tools/bot_send.py <файл> "#тема" --name "Отчёт.html"  — имя, под которым придёт файл
  python3 tools/bot_send.py --who                               — кому уйдёт (тип чата и имя), без отправки

TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID — из окружения или из .env в корне Рика (образец .env.example).
Бота делает хозяин у @BotFather, CHAT_ID — его личный чат с ботом. Значения не печатаются.
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rick_config  # noqa: E402


def env():
    vals = {}
    path = Path(rick_config.home()) / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Бот не подключён: впиши TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env (образец — .env.example)")
    return token, chat


def call(token, method, fields, file=None, name=None):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    if file:
        # кавычка, обратная косая и перевод строки в имени ломают заголовок — вон
        safe = "".join(c for c in (name or file.name) if c >= " " and c not in '"\\')
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                 f'filename="{safe}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += file.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    token, chat = env()
    if argv == ["--who"]:
        r = call(token, "getChat", {"chat_id": chat})
        c = r.get("result", {})
        print(r.get("ok"), c.get("type"), c.get("first_name") or c.get("title"))
        return 0
    args = list(argv)
    name = None
    if "--name" in args:
        i = args.index("--name")
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            sys.exit("--name без имени: bot_send.py <файл> [подпись] --name \"<имя файла>\"")
        name = args[i + 1]
        del args[i:i + 2]
    if not args:
        sys.exit("Нет файла: bot_send.py <файл> [подпись] [--name <имя>]")
    path = Path(args[0])
    if not path.is_file():
        sys.exit(f"Нет файла: {path}")
    caption = args[1] if len(args) > 1 else ""
    r = call(token, "sendDocument", {"chat_id": chat, "caption": caption, "disable_notification": "true"}, path, name)
    print("ушло" if r.get("ok") else f"не ушло: {r.get('description')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
