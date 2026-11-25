"""
A half-time card on request, for a match that doesn't post one by default.

A fixture with "post_ht": false still gets asked about at the half-time
whistle — sometimes the first half is the story. The worker sends a Telegram
question with two buttons; the bot, in another process, records the tap on
Cloudinary; the worker reads it on its next poll and, on a yes, posts the HT
card exactly as a post_ht fixture would. Once the second half starts the
question is closed: a half-time card is stale by then.

A yes leads on to the background: the bot asks for a photo, with a button
for the standard template. The answer is only recorded once that is settled —
a photo uploaded as the match's HT photo, or the template chosen — so the
worker never posts a card while the photo is still on its way.

Buttons:   ht:<match_id>:post   /   ht:<match_id>:skip   /   htbg:<match_id>
State:     optin/ht/<match_id>.json   {answer, style, answered_at}
           style: 'photo' or 'template' on a yes
"""

import io
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

PREFIX = 'ht'
TEMPLATE_PREFIX = 'htbg'
FOLDER = 'optin/ht'
ANSWERS = ('post', 'skip')
STYLES = ('photo', 'template')
# A photo arriving later than this after the yes is for a second half that has
# started — the question closes then, so the bot says so instead of uploading.
PHOTO_WINDOW_SECS = 20 * 60


def callback(match_id: str, answer: str) -> str:
    return f'{PREFIX}:{match_id}:{answer}'


def parse_callback(data: str) -> tuple[str, str] | None:
    """'ht:54577212:post' → ('54577212', 'post'). None if malformed."""
    parts = str(data or '').split(':')
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or parts[2] not in ANSWERS:
        return None
    return parts[1], parts[2]


def template_callback(match_id: str) -> str:
    return f'{TEMPLATE_PREFIX}:{match_id}'


def parse_template_callback(data: str) -> str | None:
    parts = str(data or '').split(':')
    if len(parts) != 2 or parts[0] != TEMPLATE_PREFIX or not parts[1]:
        return None
    return parts[1]


def background_prompt() -> str:
    return ("📸 Send the background photo for the half-time card now — as a "
            "photo, or as a file for full quality.\n\n"
            "Or tap the button to use the standard template.")


def options(match_id: str) -> list[tuple[str, str]]:
    return [('📸 Post the HT card', callback(match_id, 'post')),
            ('No thanks', callback(match_id, 'skip'))]


def question_text(label: str, score: str) -> str:
    return (f"🙋 {label} — half time, {score}.\n\n"
            f"This match isn't set to post a half-time card. Want one anyway?\n\n"
            f"Tap Post and I'll ask for a background photo — or you can use "
            f"the standard template. The question closes when the second half "
            f"starts.")


def closed_text(label: str) -> str:
    return (f"⏱ {label}: the second half has started, so there's no half-time "
            f"card for this one.")


def answered_text(answer: str, style: str | None = None) -> str:
    if answer != 'post':
        return "OK — no half-time card for this one."
    how = {'photo': ' with your photo',
           'template': ' on the standard template'}.get(style, '')
    return f"✅ Posting the half-time card{how} — it goes up within a minute."


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


def read_answer(match_id: str) -> dict | None:
    """{'answer': 'post'|'skip', 'style': ...}, or None when nobody has
    answered yet (or it can't be read)."""
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(_public_id(match_id), resource_type='raw')
        response = requests.get(resource['secure_url'],
                                params={'_': int(datetime.now().timestamp())},
                                timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if payload.get('answer') in ANSWERS else None
    except Exception:
        return None


def write_answer(match_id: str, answer: str, style: str | None = None) -> None:
    """Record the decision. Raises on failure, so the bot can say so."""
    cloudinary = _cloudinary()
    payload = {'answer': answer, 'style': style,
               'answered_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    blob = io.BytesIO(json.dumps(payload).encode())
    cloudinary.uploader.upload(blob, resource_type='raw',
                               public_id=_public_id(match_id),
                               overwrite=True, invalidate=True)


def record_skip(data: str) -> str:
    """The bot's side of "No thanks": store it, return the message to show."""
    parsed = parse_callback(data)
    if parsed is None or parsed[1] != 'skip':
        return "That button doesn't make sense to me any more."
    write_answer(parsed[0], 'skip')
    return answered_text('skip')
