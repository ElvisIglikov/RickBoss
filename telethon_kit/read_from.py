#!/usr/bin/env python3
"""Последние сообщения чата от своего аккаунта (юзербот), новые сверху.

    python3 read_from.py <id|@username|алиас> [--limit N]

Строка: `#id [дд.мм чч:мм] Кто: текст <вложение> ↩️ ответ на #N`. Хвост «ответ на» не пропускай —
без него ответ читается неверно. Алиасы — общие с send_to.py (aliases в team_config.json).
"""
import argparse
import asyncio

from send_to import resolve_recipient
from telegram_user_client import MY_ID, client, disconnect, ensure_connected


async def sender_name(msg, cache):
    if msg.sender_id not in cache:
        s = await msg.get_sender()
        cache[msg.sender_id] = (" ".join(filter(None, [getattr(s, "first_name", None), getattr(s, "last_name", None)]))
                                or getattr(s, "title", None) or str(msg.sender_id))
    return cache[msg.sender_id]


async def read_from(name_or_id, limit=10):
    try:
        recipient, display = resolve_recipient(name_or_id)
    except ValueError:
        recipient, display = name_or_id, name_or_id  # @username — Telethon найдёт сам
    await ensure_connected()
    entity = await client.get_entity(recipient)
    print(f"CHAT={display} ID={getattr(entity, 'id', recipient)}")
    names = {}
    async for msg in client.iter_messages(entity, limit=limit):
        who = "Я" if MY_ID and msg.sender_id == MY_ID else await sender_name(msg, names)
        text = (msg.message or "").replace("\n", " ⏎ ")
        if msg.file:
            text = f"{text} <{msg.file.name or msg.file.mime_type or 'вложение'}>".strip()
        if msg.reply_to_msg_id:
            text += f" ↩️ ответ на #{msg.reply_to_msg_id}"
        print(f"#{msg.id} [{msg.date.astimezone():%d.%m %H:%M}] {who}: {text}")


def main():
    p = argparse.ArgumentParser(description="Прочитать последние сообщения чата")
    p.add_argument("recipient", help="числовой id, @username или алиас из team_config.json")
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()

    async def run():
        try:
            await read_from(args.recipient, args.limit)
        finally:
            await disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    main()
