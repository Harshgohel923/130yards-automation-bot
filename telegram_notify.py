# telegram_notify.py — fire-and-forget pipeline alerts to your Telegram.
# Reuses the photo-intake bot's token; messages go to TELEGRAM_ALERT_CHAT_ID,
# falling back to the first id in TELEGRAM_ALLOWED_USER_IDS (your private chat
# with the bot). Never raises — an alert failure must not break a pipeline.

import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_last_sent: dict[str, float] = {}
_THROTTLE_LOCK = threading.Lock()


def _chat_id() -> str | None:
    explicit = os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip()
    if explicit:
        return explicit
    allowed = os.getenv('TELEGRAM_ALLOWED_USER_IDS', '').strip()
    if allowed:
        return allowed.split(',')[0].strip()
    return None


def send_alert(text: str, key: str | None = None, cooldown: int = 0) -> None:
    """
    Send an alert to Telegram. When `cooldown` (seconds) is set, repeated
    alerts with the same `key` (defaults to the text itself) within that
    window are dropped — use for failures that recur every poll.
    """
    if cooldown:
        k = key or text
        now = time.time()
        with _THROTTLE_LOCK:
            if now - _last_sent.get(k, 0.0) < cooldown:
                return
            _last_sent[k] = now

    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = _chat_id()
    if not token or not chat_id:
        print('[telegram_notify] Not configured (need TELEGRAM_BOT_TOKEN and '
              'TELEGRAM_ALERT_CHAT_ID or TELEGRAM_ALLOWED_USER_IDS) — alert dropped:')
        print(f'[telegram_notify]   {text}')
        return
    try:
        res = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
        if not res.ok:
            print(f'[telegram_notify] sendMessage failed '
                  f'(HTTP {res.status_code}): {res.text[:200]}')
    except Exception as e:
        print(f'[telegram_notify] sendMessage error: {e}')
