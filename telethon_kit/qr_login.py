#!/usr/bin/env python3
"""Вход юзербота по QR-коду (когда код входа не приходит). Запускает ХОЗЯИН в своём терминале:
    .venv/bin/python qr_login.py
Телефон: Telegram → Настройки → Устройства → Подключить устройство → навести на QR в терминале.
Пароль 2FA (если включён) вводится здесь же, скрыто. Создаёт user_session.session, как reauth_telegram.py.
"""
import asyncio
import getpass

import qrcode
from telethon.errors import SessionPasswordNeededError

from telegram_user_client import client


def show(url):
    q = qrcode.QRCode(border=2)
    q.add_data(url)
    print("\033[2J\033[HTelegram → Настройки → Устройства → Подключить устройство — наведи камеру:\n")
    q.print_ascii(invert=True)


async def main():
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже вошли: {me.first_name} (id {me.id})")
        return await client.disconnect()
    qr = await client.qr_login()
    while True:
        show(qr.url)
        try:
            await qr.wait(timeout=25)
            break
        except asyncio.TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Пароль двухэтапной проверки Telegram: "))
            break
    me = await client.get_me()
    print(f"\nOK — вошли как {me.first_name or ''} (id {me.id}). Впиши id в .env как TELEGRAM_MY_ID.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
