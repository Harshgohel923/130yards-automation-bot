"""
A half-time card on request, for a match that doesn't post one by default.

A fixture with "post_ht": false still gets asked about at the half-time
whistle — sometimes the first half is the story. The worker sends a Telegram
question with two buttons; the bot, in another process, records the tap on
Cloudinary; the worker reads it on its next poll and, on a yes, posts the HT
card exactly as a post_ht fixture would. Once the second half starts the
question is closed: a half-time card is stale by then.

Buttons:   ht:<match_id>:post   /   ht:<match_id>:skip
State:     optin/ht/<match_id>.json   {answer, answered_at}
"""

import io
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

PREFIX = 'ht'
FOLDER = 'optin/ht'
ANSWERS = ('post', 'skip')


def callback(match_id: str, answer: str) -> str:
    return f'{PREFIX}:{match_id}:{answer}'


def parse_callback(data: str) -> tuple[str, str] | None:
    """'ht:54577212:post' → ('54577212', 'post'). None if malformed."""
    parts = str(data or '').split(':')
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or parts[2] not in ANSWERS:
        return None
    return parts[1], parts[2]


def options(match_id: str) -> list[tuple[str, str]]:
    return [('📸 Post the HT card', callback(match_id, 'post')),
            ('No thanks', callback(match_id, 'skip'))]


def question_text(label: str, score: str) -> str:
    return (f"🙋 {label} — half time, {score}.\n\n"
            f"This match isn't set to post a half-time card. Want one anyway?\n\n"
            f"Tap Post and it goes up within a minute. If you want your own "
            f"photo on it, send it with /start first, then tap Post. The "
            f"question closes when the second half starts.")


def closed_text(label: str) -> str:
    return (f"⏱ {label}: the second half has started, so there's no half-time "
            f"card for this one.")


def answered_text(answer: str) -> str:
    if answer == 'post':
        return "✅ Posting the half-time card — it goes up within a minute."
    return "OK — no half-time card for this one."


# ── State on Cloudinary ──────────────────────────────────────────────────────

def _cloudinary():
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    )
    return cloudinary


def _public_id(match_id: str) -> str:
    return f'{FOLDER}/{match_id}.json'


def read_answer(match_id: str) -> str | None:
    """'post', 'skip', or None when nobody has answered (or it can't be read)."""
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(_public_id(match_id), resource_type='raw')
        response = requests.get(resource['secure_url'],
                                params={'_': int(datetime.now().timestamp())},
                                timeout=15)
        response.raise_for_status()
        answer = response.json().get('answer')
        return answer if answer in ANSWERS else None
    except Exception:
        return None


def write_answer(match_id: str, answer: str) -> None:
    """Record the tap. Raises on failure, so the bot can say so."""
    cloudinary = _cloudinary()
    payload = {'answer': answer,
               'answered_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    blob = io.BytesIO(json.dumps(payload).encode())
    cloudinary.uploader.upload(blob, resource_type='raw',
                               public_id=_public_id(match_id),
                               overwrite=True, invalidate=True)


def record_tap(data: str) -> str:
    """The bot's side: store one tap and return the message to show."""
    parsed = parse_callback(data)
    if parsed is None:
        return "That button doesn't make sense to me any more."
    match_id, answer = parsed
    write_answer(match_id, answer)
    return answered_text(answer)
