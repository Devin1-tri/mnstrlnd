#!/usr/bin/env python3
"""add_account.py — register a new Telegram account for Monsterland multi-account.

Usage:
  python add_account.py <name> <phone>
  e.g. python add_account.py alt1 +62812xxxxxxx

Flow: OTP login via Telethon (interactive, supports 2FA password).
Session saved to sessions/<name>.session, registered in accounts.json.
Uses the same TG_API_ID/HASH from ~/.tg_cred.env (app credentials are
app-level, reusable across accounts).
"""
import asyncio, sys
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from monster_api import load_creds, register_account, load_accounts, BOT_DIR

SESSION_DIR = BOT_DIR / 'sessions'


async def login(name, phone):
    c = load_creds()
    SESSION_DIR.mkdir(exist_ok=True)
    sess = SESSION_DIR / f'{name}.session'

    client = TelegramClient(str(sess), int(c['TG_API_ID']), c['TG_API_HASH'])
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f'[OK] session already authorized: @{me.username or me.first_name} ({me.id})')
            return me

        print(f'Sending OTP to {phone}...')
        await client.send_code_request(phone)
        code = input('Enter OTP code: ').strip()

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pw = input('2FA password: ').strip()
            await client.sign_in(password=pw)

        me = await client.get_me()
        print(f'[OK] logged in: @{me.username or me.first_name} (id={me.id})')
        return me
    finally:
        await client.disconnect()


def main():
    if len(sys.argv) < 3:
        print('Usage: python add_account.py <name> <phone>')
        print('  e.g. python add_account.py alt1 +62812xxxxxxx')
        sys.exit(1)

    name = sys.argv[1].strip()
    phone = sys.argv[2].strip()

    existing = {a.name for a in load_accounts()}
    if name in existing:
        print(f'[!] account "{name}" already registered — re-login will overwrite session')

    me = asyncio.run(login(name, phone))

    c = load_creds()
    sess = SESSION_DIR / f'{name}.session'
    register_account(name, sess, phone, c['TG_API_ID'], c['TG_API_HASH'])
    print(f'[OK] account "{name}" registered -> accounts.json')
    print(f'     session: {sess}.session')
    print(f'     user:    @{me.username or me.first_name} (id={me.id})')
    print(f'     phone:   {phone}')
    print()
    print('Next: run monster_live.py — it will farm all enabled accounts.')


if __name__ == '__main__':
    main()
