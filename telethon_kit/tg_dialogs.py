#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Список диалогов Telegram с ID — чтобы найти группу или человека для read_from и send_to.

Одна задача: один вызов iter_dialogs → строки KEY=value. Ничего не читает внутри чатов.
ID здесь уже в том виде, который принимают send_message / iter_messages:
    пользователь      123456789        (положительный)
    обычная группа    -4123456789      (отрицательный)
    супергруппа/канал -100XXXXXXXXXX   (отрицательный с префиксом -100)
После этого прогона все сущности лежат в кеше сессии, и обращение по числовому ID
работает без ошибки «Could not find the input entity».

Usage:
    python3 tg_dialogs.py                       # все диалоги (до --limit 300)
    python3 tg_dialogs.py --filter работа       # по подстроке названия (без регистра)
    python3 tg_dialogs.py --kind group          # dm | bot | group | channel
    python3 tg_dialogs.py --unread              # только где есть непрочитанное
    python3 tg_dialogs.py --json                # JSON-массив вместо KEY=value
"""
import argparse
import asyncio
import json

from telethon.tl.types import Channel, Chat, User

from telegram_user_client import client, ensure_connected, disconnect


def dialog_kind(entity) -> str:
    if isinstance(entity, User):
        return "bot" if getattr(entity, "bot", False) else "dm"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return "forum" if getattr(entity, "forum", False) else "group"
        return "channel"
    return "other"


async def list_dialogs(limit, name_filter, kind_filter, unread_only):
    await ensure_connected()
    rows = []
    async for dialog in client.iter_dialogs(limit=limit):
        kind = dialog_kind(dialog.entity)
        if kind_filter and kind != kind_filter and not (kind_filter == "group" and kind == "forum"):
            continue
        if unread_only and not dialog.unread_count:
            continue
        if name_filter and name_filter.lower() not in (dialog.name or "").lower():
            continue
        rows.append({
            "id": dialog.id,
            "name": dialog.name,
            "kind": kind,
            "username": getattr(dialog.entity, "username", None),
            "unread": dialog.unread_count,
            "last": dialog.date.astimezone().strftime("%Y-%m-%d %H:%M") if dialog.date else None,
            "members": getattr(dialog.entity, "participants_count", None),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description="Список диалогов Telegram с ID")
    parser.add_argument("--limit", type=int, default=300, help="сколько диалогов просмотреть (по свежести)")
    parser.add_argument("--filter", default="", help="подстрока названия чата")
    parser.add_argument("--kind", choices=["dm", "bot", "group", "channel"], default=None)
    parser.add_argument("--unread", action="store_true", help="только с непрочитанным")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    async def run():
        try:
            return await list_dialogs(args.limit, args.filter, args.kind, args.unread)
        finally:
            await disconnect()

    rows = asyncio.run(run())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for row in rows:
        username = f" @{row['username']}" if row["username"] else ""
        print(f"ID={row['id']} KIND={row['kind']} UNREAD={row['unread']} LAST={row['last']} "
              f"NAME={row['name']}{username}")
    print(f"DIALOGS={len(rows)}")


if __name__ == "__main__":
    main()
