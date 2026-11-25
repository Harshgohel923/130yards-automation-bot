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
                key: str | None = None, cooldown: int = 0) -> int | None:
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

    Returns the sent message's id, so the sender can edit it later (see
    edit_message), or None if nothing went out.
    """
    keyboard = {'inline_keyboard': [[{'text': label, 'callback_data': data}]
                                    for label, data in options]}
    return _send(text, key=key, cooldown=cooldown,
                 reply_markup=json.dumps(keyboard))


def _preview_url(url: str) -> str:
    """A phone-sized JPEG of a Cloudinary image. Telegram fetches photos by URL
    only up to 5 MB, and a full-size PNG scorecard can be close to that."""
    marker = '/image/upload/'
    if marker in url and '/w_' not in url:
        return url.replace(marker, f'{marker}w_1080,q_auto,f_jpg/', 1)
    return url


def send_photos(items: list[tuple[str, str]]) -> bool:
    """
    Send images by URL, each with a short caption: [(url, caption), ...].

    Two or more go as one album (Telegram allows up to 10, the same cap a
    carousel has); one goes as a plain photo. Albums can't carry buttons, so a
    question about them follows as its own message. Never raises.
    """
    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = _chat_id()
    if not token or not chat_id or not items:
        return False
    try:
        if len(items) == 1:
            url, caption = items[0]
            res = requests.post(
                f'https://api.telegram.org/bot{token}/sendPhoto',
                data={'chat_id': chat_id, 'photo': _preview_url(url),
                      'caption': caption},
                timeout=30)
        else:
            media = [{'type': 'photo', 'media': _preview_url(url),
                      'caption': caption} for url, caption in items[:10]]
            res = requests.post(
                f'https://api.telegram.org/bot{token}/sendMediaGroup',
                data={'chat_id': chat_id, 'media': json.dumps(media)},
                timeout=60)
        if not res.ok:
            print(f'[telegram_notify] sending photos failed '
                  f'(HTTP {res.status_code}): {res.text[:200]}')
        return res.ok
    except Exception as e:
        print(f'[telegram_notify] sending photos error: {e}')
        return False


def send_news(text: str, preview: bool = False) -> bool:
    """
    A news message (see news.py). Goes to TELEGRAM_NEWS_CHAT_ID when set — so
    the news can live in its own group or topic — and to the alerts chat
    otherwise. The text is HTML (bold headlines, short "Read on …" links).
    Link previews are off unless asked for: a digest of five links would
    otherwise be five preview cards. Never raises.
    """
    return _send_html(text, os.getenv('TELEGRAM_NEWS_CHAT_ID', '').strip() or _chat_id(),
                      preview)


def send_html(text: str, preview: bool = False) -> bool:
    """An HTML message to the alerts chat — the Weekly Recap. Never raises."""
    return _send_html(text, _chat_id(), preview)


def _send_html(text: str, chat_id: str | None, preview: bool) -> bool:
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token or not chat_id:
        print(f'[telegram_notify] Not configured — message dropped:\n{text}')
        return False
    try:
        res = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': chat_id, 'text': text[:4096], 'parse_mode': 'HTML',
                  'disable_web_page_preview': 'false' if preview else 'true'},
            timeout=10)
        if not res.ok:
            print(f'[telegram_notify] HTML send failed (HTTP {res.status_code}): '
                  f'{res.text[:200]}')
        return res.ok
    except Exception as e:
        print(f'[telegram_notify] HTML send error: {e}')
        return False


def edit_message(message_id: int, text: str) -> bool:
    """
    Replace the text of a message this bot sent, removing its buttons.

    For a question the sender has stopped waiting on — the buttons must not
    stay tappable once the answer can no longer change anything. Never raises.
    """
    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = _chat_id()
    if not token or not chat_id or not message_id:
        return False
    try:
        res = requests.post(
            f'https://api.telegram.org/bot{token}/editMessageText',
            data={'chat_id': chat_id, 'message_id': message_id, 'text': text,
                  'reply_markup': json.dumps({'inline_keyboard': []})},
            timeout=10,
        )
        if not res.ok:
            print(f'[telegram_notify] editMessageText failed '
                  f'(HTTP {res.status_code}): {res.text[:200]}')
        return res.ok
    except Exception as e:
        print(f'[telegram_notify] editMessageText error: {e}')
        return False


def _send(text: str, key: str | None = None, cooldown: int = 0,
          reply_markup: str | None = None) -> int | None:
    """The one place anything is actually sent. Never raises. Returns the
    message id when Telegram accepted it."""
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
            return None
        return (res.json().get('result') or {}).get('message_id')
    except Exception as e:
        print(f'[telegram_notify] sendMessage error: {e}')
        return None


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
