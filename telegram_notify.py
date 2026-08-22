# telegram_notify.py — fire-and-forget pipeline alerts to your Telegram.
# Reuses the photo-intake bot's token; messages go to TELEGRAM_ALERT_CHAT_ID,
# falling back to the first id in TELEGRAM_ALLOWED_USER_IDS (your private chat
# with the bot). Never raises — an alert failure must not break a pipeline.

import json
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
    _send(text, key=key, cooldown=cooldown)


def send_choice(text: str, options: list[tuple[str, str]],
                key: str | None = None, cooldown: int = 0) -> None:
    """
    An alert with tappable answers: [(button label, callback_data), ...].

    The tap is handled by telegram_bot.py, in a different process that shares
    nothing with this one — so `callback_data` has to be something the bot can
    resolve from scratch, and it has to fit inside Telegram's 64-byte cap. See
    event_photos.digest for what goes in it.

    One button per row: these are player names, and two names side by side on a
    phone truncate to the point where the choice is between two prefixes.

    A question nobody can answer is still worth asking, so a failure to attach
    the keyboard is never fatal — the text goes either way.
    """
    keyboard = {'inline_keyboard': [[{'text': label, 'callback_data': data}]
                                    for label, data in options]}
    _send(text, key=key, cooldown=cooldown,
          reply_markup=json.dumps(keyboard))


def _send(text: str, key: str | None = None, cooldown: int = 0,
          reply_markup: str | None = None) -> None:
    """The one place anything is actually sent. Never raises."""
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
    payload = {'chat_id': chat_id, 'text': text}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        res = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data=payload,
            timeout=10,
        )
        if not res.ok:
            print(f'[telegram_notify] sendMessage failed '
                  f'(HTTP {res.status_code}): {res.text[:200]}')
    except Exception as e:
        print(f'[telegram_notify] sendMessage error: {e}')


def send_music_reminder(description: str, media_id: str | None = None) -> None:
    """
    Nudge to add music to a post that has just gone live.

    Instagram's publishing API can't attach audio, so every post the bot makes
    goes up silent and someone has to add the track by hand — easy to forget
    once the match is over, hence the reminder, with a tappable link straight
    to the post.

    Never raises and is never throttled: one post, one reminder.
    """
    link = None
    if media_id:
        # Imported here rather than at module level: this module is used by
        # config.py and the renderers, which have no business importing the
        # Instagram client.
        try:
            from instagram import get_post_permalink
            link = get_post_permalink(media_id)
        except Exception as e:
            print(f'[telegram_notify] Could not look up the post link: {e}')

    send_alert(
        f"🎵 Just posted — remember to add the music.\n\n"
        f"{description}\n"
        f"{link or '(open the page to find it — the link lookup failed)'}\n\n"
        f"Instagram won't let the bot add audio, so it has to be done by hand "
        f"in the app."
    )
