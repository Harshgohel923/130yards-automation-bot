# find_chat_id.py — discover the chat id to use for TELEGRAM_ALERT_CHAT_ID.
"""
Lists every chat the bot has recently seen, so you can copy the id of your
alerts group.

Usage:
    1. Create a Telegram group and add the people who should get alerts.
    2. Add the bot to that group.
    3. Send "/start@<your_bot_username>" in the group (bots with privacy mode
       on only see commands and mentions, so a plain "hi" may not register).
    4. python find_chat_id.py

Group ids are negative (e.g. -1001234567890) — that is normal, copy it whole.
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        print('TELEGRAM_BOT_TOKEN is not set in .env')
        return 1

    try:
        res = requests.get(f'https://api.telegram.org/bot{token}/getUpdates',
                           timeout=15)
        res.raise_for_status()
        updates = res.json().get('result', [])
    except Exception as e:
        print(f'Could not reach Telegram: {e}')
        return 1

    # Any update type can carry a chat — messages, joins, membership changes.
    chats = {}
    for upd in updates:
        for payload in upd.values():
            if isinstance(payload, dict):
                chat = payload.get('chat') or {}
                if chat.get('id') is not None:
                    chats[chat['id']] = chat

    if not chats:
        print('No chats seen. Telegram only keeps ~24h of updates, and a bot\n'
              'with privacy mode on needs a command or mention to register.\n'
              'Send "/start@<your_bot_username>" in the group, then re-run.')
        return 1

    print(f'{"chat id":<18} {"type":<12} name')
    print('-' * 60)
    for cid, chat in sorted(chats.items()):
        name = (chat.get('title')
                or ' '.join(filter(None, (chat.get('first_name'),
                                          chat.get('last_name'))))
                or chat.get('username') or '')
        print(f'{cid:<18} {chat.get("type", "?"):<12} {name}')

    groups = [c for c in chats.values() if str(c.get('type', '')).endswith('group')]
    if groups:
        print(f'\nUse the group id above as TELEGRAM_ALERT_CHAT_ID '
              f'(locally in .env and as a GitHub Actions secret).')
    else:
        print('\nNo group found yet — only private chats are listed above.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
