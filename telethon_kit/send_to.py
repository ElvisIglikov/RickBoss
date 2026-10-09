#!/usr/bin/env python3
"""Отправить сообщение или файл от своего аккаунта (юзербот) — только с «да» хозяина.

Без --yes ничего не уходит: скрипт печатает черновик (получатель, текст, файл) и выходит с кодом 2.
--yes ставится, только когда хозяин в чате с Рика сказал «да» именно на этот текст и получателя.

    python3 send_to.py 123456789 "текст"                       — показать черновик
    python3 send_to.py 123456789 --stdin --yes < draft.txt      — отправить (длинный текст — только --stdin)
    python3 send_to.py ivan --file отчёт.pdf "подпись" --yes    — файл документом, алиас из team_config.json
    python3 send_to.py 123456789 "текст" --reply-to 12345 --yes  — ответ цитатой

Перед отправкой — проверки send_hooks.py; все вызовы идут через защитный слой telegram_safety.py.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from send_hooks import (ALIASES, clean_wrapper_text, format_send_confirmation, pre_send_check,  # noqa: E402
                        resolve_name_by_id)

# Свои люди и группы: имя → id (tg_dialogs.py) — раздел aliases в team_config.json, в коде пусто.
STATIC_ALIASES = ALIASES
STATIC_ALIASES_INV = {v: k for k, v in STATIC_ALIASES.items()}


def resolve_recipient(name_or_id):
    """Вернуть (id, имя для показа)."""
    try:
        rid = int(name_or_id)
        return rid, resolve_name_by_id("telegram", rid) or STATIC_ALIASES_INV.get(rid) or str(rid)
    except (ValueError, TypeError):
        pass
    key = name_or_id.strip().lower()
    if key in STATIC_ALIASES:
        rid = STATIC_ALIASES[key]
        return rid, resolve_name_by_id("telegram", rid) or name_or_id
    raise ValueError(f"Неизвестный алиас '{name_or_id}': дай числовой id или добавь его в aliases team_config.json")


async def send_to(name_or_id, text, confirmed=False, abort_on_warnings=False, file=None, reply_to=None,
                  schedule=None, photo=False):
    """Отправить; вернуть id сообщения. Без confirmed=True — исключение: «да» хозяина не получено."""
    recipient_id, display_name = resolve_recipient(name_or_id)
    text = clean_wrapper_text(text)
    ok, warnings = pre_send_check(platform="telegram", recipient_id=recipient_id,
                                  recipient_name=display_name, text=text)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if not ok:
        raise RuntimeError(f"pre_send_check не пропустил {display_name} ({recipient_id}) — не отправляю")
    if warnings and abort_on_warnings:
        raise RuntimeError("есть предупреждения, а стоит --strict")
    if file and not os.path.exists(file):
        raise FileNotFoundError(f"нет файла: {file}")
    if not confirmed:
        raise PermissionError("нет «да» хозяина (--yes): " + format_send_confirmation("telegram", recipient_id, text)
                              + (f" + файл {os.path.basename(file)}" if file else ""))

    from telegram_user_client import client, ensure_connected  # клиент — только когда точно отправляем
    await ensure_connected()
    if file:
        msg = await client.send_file(recipient_id, file, caption=text, force_document=not photo,
                                     reply_to=reply_to, silent=True, schedule=schedule)
        print(f"OK -> {display_name} ({recipient_id}) file={os.path.basename(file)} msg_id={msg.id}")
    else:
        msg = await client.send_message(recipient_id, text, reply_to=reply_to, silent=True, schedule=schedule)
        print(f"OK -> {display_name} ({recipient_id}) msg_id={msg.id}")
    return msg.id


def main():
    p = argparse.ArgumentParser(description="Отправка в Telegram от своего аккаунта — только с --yes")
    p.add_argument("recipient", help="числовой id или алиас из team_config.json")
    p.add_argument("text", nargs="?", help="текст (или --stdin)")
    p.add_argument("--stdin", action="store_true", help="текст из stdin")
    p.add_argument("--yes", action="store_true", help="хозяин сказал «да» на этот текст и получателя")
    p.add_argument("--strict", action="store_true", help="не отправлять при любом предупреждении")
    p.add_argument("--file", help="файл документом (текст станет подписью)")
    p.add_argument("--photo", action="store_true", help="с --file: картинка картинкой, а не файлом")
    p.add_argument("--reply-to", type=int, default=None, help="id сообщения, на которое отвечаем")
    p.add_argument("--schedule", default=None, help="отложенная отправка: 'YYYY-MM-DD HH:MM' по времени Мака")
    args = p.parse_args()

    if args.stdin:
        text = sys.stdin.read()
    elif args.text:
        text = args.text
    elif args.file:
        text = ""
    else:
        p.error("дай текст аргументом или --stdin")

    schedule = datetime.strptime(args.schedule, "%Y-%m-%d %H:%M").astimezone() if args.schedule else None
    try:
        msg_id = asyncio.run(send_to(args.recipient, text, confirmed=args.yes, abort_on_warnings=args.strict,
                                     file=args.file, reply_to=args.reply_to, schedule=schedule, photo=args.photo))
        print(f"MSG_ID={msg_id}")
    except PermissionError as e:
        print(f"DRAFT: {e}\n---\n{text}\n---\nПокажи черновик хозяину; после его «да» — тот же вызов с --yes.")
        sys.exit(2)
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
