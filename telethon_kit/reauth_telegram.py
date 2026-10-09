#!/usr/bin/env python3
"""Первый вход юзербота (файл user_session.session). Запускает ХОЗЯИН в своём терминале — нужен код из Telegram:
    .venv/bin/python reauth_telegram.py

Спросит телефон, код из Telegram и пароль двухэтапной проверки (если включён).
В конце печатает твой id — впиши его в .env как TELEGRAM_MY_ID.
"""
import asyncio

from telegram_user_client import client


async def main():
    await client.start()  # телефон → код → (пароль 2FA)
    me = await client.get_me()
    print(f"\nOK — вошли как {me.first_name or ''} (id {me.id}). Впиши id в .env как TELEGRAM_MY_ID.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
