"""
"The scoreline has changed since your photo — new photo, or the old one?"

A background photo sent for the HT or FT card shows the match as it stood: a
side celebrating a lead, a side on top. Late on — from 35' for the half-time
card, from 80' for the full-time one — the worker notes who is ahead the
moment there is a photo to compare against. If that changes before the card
goes out (a lead lost, a lead taken, a comeback) the photo may now show the
wrong story, so the worker asks on Telegram:

    📸 Upload new photo   /   Post the older one

and holds the card — early posts and corrections included — until:
  • "Post the older one" is tapped                       → post now
  • nobody answers for ANSWER_WAIT_SECS (2 minutes)      → post with the old photo
  • "Upload new photo" is tapped and a photo arrives     → post with the new one
  • ... but none arrives within PHOTO_WAIT_SECS (10 min) → post with the old photo

A goal that only stretches a lead (2-0 to 3-0) doesn't change who is ahead,
so it doesn't ask.

The worker asks and waits; the bot handles the taps and the upload. They
share this module and a small JSON on Cloudinary per question. Each question
has an id (its ask time) carried in its buttons, so a tap on an older
question can never answer a newer one.

Buttons:   st:<match_id>:<HT|FT>:<qid>:new   /   …:old
State:     story/<match_id>_<HT|FT>.json
           {qid, answer, answered_ts, uploaded, closed}
"""

import io
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PREFIX = 'st'
FOLDER = 'story'
ANSWER_WAIT_SECS = 2 * 60
PHOTO_WAIT_SECS = 10 * 60

# From when the photo has to still tell the story, per card.
WATCH_FROM_MINUTE = {'HT': 35, 'FT': 80}


def leader(home, away) -> str | None:
    """'home', 'away' or 'level' — who is ahead. None if the score is unreadable."""
    try:
        h, a = int(home), int(away)
    except (TypeError, ValueError):
        return None
    return 'home' if h > a else 'away' if a > h else 'level'


def describe(home_team: str, away_team: str, score) -> str:
    """'Arsenal 1-0 Chelsea (Arsenal ahead)' / '… (level)'."""
    h, a = score
    who = leader(h, a)
    state = {'home': f'{home_team} ahead', 'away': f'{away_team} ahead',
             'level': 'level'}.get(who, '')
    return f"{home_team} {h}-{a} {away_team}" + (f" ({state})" if state else '')


# ── Buttons ──────────────────────────────────────────────────────────────────

def callback(match_id: str, moment: str, qid: int, choice: str) -> str:
    return f'{PREFIX}:{match_id}:{moment}:{qid}:{choice}'


def parse_callback(data: str) -> tuple[str, str, int, str] | None:
    parts = str(data or '').split(':')
    if (len(parts) != 5 or parts[0] != PREFIX or not parts[1]
            or parts[2] not in ('HT', 'FT') or not parts[3].isdigit()
            or parts[4] not in ('new', 'old')):
        return None
    return parts[1], parts[2], int(parts[3]), parts[4]


def options(match_id: str, moment: str, qid: int) -> list[tuple[str, str]]:
    return [('📸 Upload new photo', callback(match_id, moment, qid, 'new')),
            ('Post the older one', callback(match_id, moment, qid, 'old'))]


def moment_name(moment: str) -> str:
    return 'half-time' if moment == 'HT' else 'full-time'


def question_text(label: str, moment: str, before: str, now: str) -> str:
    return (f"🔄 {label}: the scoreline has changed since the {moment_name(moment)} "
            f"photo was sent.\n\n"
            f"Then: {before}\nNow: {now}\n\n"
            f"Upload a new photo, or post with the older one? If nobody "
            f"answers in 2 minutes, the older photo is used.")


def resolved_text(reason: str) -> str:
    return {
        'old':      "Posting with the older photo.",
        'uploaded': "✅ New photo in — the card uses it.",
        'no-answer': "⏱ No answer in 2 minutes — posting with the older photo.",
        'no-photo': "⏱ No new photo in 10 minutes — posting with the older photo.",
    }.get(reason, "Posting with the older photo.")


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


def _public_id(match_id: str, moment: str) -> str:
    return f'{FOLDER}/{match_id}_{moment}.json'


def read_state(match_id: str, moment: str) -> dict | None:
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(_public_id(match_id, moment),
                                           resource_type='raw')
        response = requests.get(resource['secure_url'],
                                params={'_': int(time.time())}, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def write_state(match_id: str, moment: str, state: dict) -> None:
    """Replace the question's state. Raises on failure."""
    cloudinary = _cloudinary()
    blob = io.BytesIO(json.dumps(state).encode())
    cloudinary.uploader.upload(blob, resource_type='raw',
                               public_id=_public_id(match_id, moment),
                               overwrite=True, invalidate=True)


# ── The bot's side ───────────────────────────────────────────────────────────

def record_choice(data: str) -> tuple[str, dict | None]:
    """
    Apply a tap. Returns (message to show, state) — state None when the tap
    can no longer change anything (an old question, or the card already went).
    """
    parsed = parse_callback(data)
    if parsed is None:
        return "That button doesn't make sense to me any more.", None
    match_id, moment, qid, choice = parsed
    state = read_state(match_id, moment) or {}
    if state.get('qid') != qid or state.get('closed'):
        return ("Too late — the card has already gone out with the photo it "
                "had."), None
    state.update(answer=choice, answered_ts=int(time.time()))
    write_state(match_id, moment, state)
    if choice == 'old':
        return resolved_text('old'), state
    return ("📸 Send the new photo now — as a photo, or as a file for full "
            "quality. I'll wait 10 minutes; after that the older one is used."), state


def record_upload(match_id: str, moment: str, qid: int) -> bool:
    """After a new photo is uploaded: mark it, unless the question has closed.
    Returns False when it is too late to make a difference."""
    state = read_state(match_id, moment) or {}
    if state.get('qid') != qid or state.get('closed'):
        return False
    state['uploaded'] = True
    write_state(match_id, moment, state)
    return True
