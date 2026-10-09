"""Ядро юзербота: читает .env рядом, создаёт клиент Telethon и ОБЯЗАТЕЛЬНО вешает защитный слой.

Все скрипты кита берут `client` отсюда. Юзербот выключен, пока в .env нет TELEGRAM_USERBOT_ENABLED=1
и ключей приложения: вход от своего аккаунта может кончиться баном, включает его только хозяин.
Сессия — user_session.session рядом с этим файлом (ключ от аккаунта: не копировать, не коммитить).
"""
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

KIT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(KIT, ".env"))

if os.getenv("TELEGRAM_USERBOT_ENABLED") != "1":
    sys.exit("Юзербот выключен: TELEGRAM_USERBOT_ENABLED=1 в telethon_kit/.env (риск бана — см. README.md)")
if not os.getenv("TELEGRAM_API_ID") or not os.getenv("TELEGRAM_API_HASH"):
    sys.exit("Нет ключей: TELEGRAM_API_ID и TELEGRAM_API_HASH в telethon_kit/.env (my.telegram.org)")

from telegram_safety import install_safety  # noqa: E402 — без защитного слоя клиента не будет

client = TelegramClient(os.path.join(KIT, "user_session"), int(os.getenv("TELEGRAM_API_ID")),
                        os.getenv("TELEGRAM_API_HASH"))
install_safety(client)
MY_ID = int(os.getenv("TELEGRAM_MY_ID") or "0")


async def ensure_connected():
    """Подключиться, если ещё нет. Не вошли — подсказать вход, а не спрашивать телефон посреди скрипта."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Не вошли в Telegram: хозяин запускает в своём терминале python reauth_telegram.py или qr_login.py")


async def disconnect():
    if client.is_connected():
        await client.disconnect()
