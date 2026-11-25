"""
Who sends the photos — a weekly count, and a Monday "Weekly Recap".

Every photo uploaded through the Telegram bot is credited to the person who
sent it: HT and FT backgrounds, staged event photos, FT carousel extras, a
new photo after the scoreline changed. Re-uploads count too — replacing a
picture after the score moved is work — and are also tallied on their own.

Counts are kept per ISO week (German time, the bot's local time), per
Telegram user id, so a renamed account doesn't split its total. Every Monday
from RECAP_HOUR the dispatcher posts last week's table to the group, once.

Storage: stats/uploads.json on Cloudinary — written by the bot (one process
at a time), read by the dispatcher.
"""

import io
import json
import os
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

STATS_ID = 'stats/uploads.json'
RECAP_WEEKDAY = 0          # Monday
RECAP_HOUR = 9             # German time
KEEP_WEEKS = 8

KINDS = ('HT', 'FT', 'event', 'extra')
KIND_LABEL = {'HT': 'HT', 'FT': 'FT', 'event': 'events', 'extra': 'carousel'}
MEDALS = ['🥇', '🥈', '🥉']


def _tz():
    from config import LOCAL_TZ
    return LOCAL_TZ


def week_key(ts: float) -> str:
    """'2026-W48' — the ISO week a moment falls in, German time."""
    year, week, _day = datetime.fromtimestamp(ts, _tz()).isocalendar()
    return f'{year}-W{week:02d}'


def week_range(key: str) -> str:
    """'2026-W48' → '23–29 Nov' (or '30 Nov – 6 Dec' across a month)."""
    year, week = key.split('-W')
    monday = datetime.fromisocalendar(int(year), int(week), 1)
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {sunday:%b}"
    return f"{monday.day} {monday:%b} – {sunday.day} {sunday:%b}"


def kind_of(public_id: str) -> str | None:
    """Which kind of photo a Cloudinary id is, or None if it isn't one we count."""
    pid = str(public_id or '')
    if pid.startswith('event_photos/'):
        return 'event'
    if pid.startswith('match_photos/'):
        if '_extra_' in pid:
            return 'extra'
        if pid.endswith('_HT'):
            return 'HT'
        if pid.endswith('_FT'):
            return 'FT'
    return None


# ── Storage ──────────────────────────────────────────────────────────────────

def _cloudinary():
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
                      api_key=os.getenv('CLOUDINARY_API_KEY'),
                      api_secret=os.getenv('CLOUDINARY_API_SECRET'))
    return cloudinary


def load() -> dict | None:
    """The stats, {} if none yet, None if they can't be read."""
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(STATS_ID, resource_type='raw')
    except Exception as e:
        return {} if 'not found' in str(e).lower() else None
    try:
        r = requests.get(resource['secure_url'], params={'_': int(time.time())}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def save(stats: dict) -> None:
    cloudinary = _cloudinary()
    blob = io.BytesIO(json.dumps(stats, ensure_ascii=False).encode())
    cloudinary.uploader.upload(blob, resource_type='raw', public_id=STATS_ID,
                               overwrite=True, invalidate=True)


# ── Counting ─────────────────────────────────────────────────────────────────

def add(stats: dict, user_id, name: str, kind: str, reupload: bool, ts: float) -> dict:
    """Count one upload into stats (pure — no I/O)."""
    week = stats.setdefault('weeks', {}).setdefault(week_key(ts), {})
    person = week.setdefault(str(user_id), {'name': name or 'someone'})
    if name:
        person['name'] = name               # the latest name they go by
    person[kind] = person.get(kind, 0) + 1
    person['total'] = person.get('total', 0) + 1
    if reupload:
        person['reuploads'] = person.get('reuploads', 0) + 1
    # Keep only the last few weeks.
    for old in sorted(stats['weeks'])[:-KEEP_WEEKS]:
        del stats['weeks'][old]
    return stats


def record_upload(user_id, name: str, public_id: str, reupload: bool,
                  ts: float | None = None) -> bool:
    """Count an upload. Returns False (and counts nothing) for ids that aren't
    match photos, or when the stats can't be read — a count must never be
    written over stats it couldn't load."""
    kind = kind_of(public_id)
    if kind is None or user_id is None:
        return False
    stats = load()
    if stats is None:
        print('[leaderboard] Could not read the stats — upload not counted.')
        return False
    save(add(stats, user_id, name, kind, reupload, ts or time.time()))
    return True


# ── The Monday recap ─────────────────────────────────────────────────────────

def recap_text(week: dict, key: str) -> str:
    """HTML for Telegram."""
    import html
    title = f"🏆 <b>Weekly Recap</b> — {week_range(key)}"
    people = sorted(week.values(), key=lambda p: (p.get('total', 0), p.get('reuploads', 0)),
                    reverse=True)
    if not people:
        return f"{title}\n\nNo photos were sent through the bot last week."
    lines = [title, '', 'Photos sent through the bot:', '']
    for n, p in enumerate(people):
        medal = MEDALS[n] if n < len(MEDALS) else f'{n + 1}.'
        parts = ' · '.join(f"{KIND_LABEL[k]} {p[k]}" for k in KINDS if p.get(k))
        redo = (f" — incl. {p['reuploads']} re-upload{'s' if p['reuploads'] != 1 else ''}"
                if p.get('reuploads') else '')
        total = p.get('total', 0)
        lines.append(f"{medal} <b>{html.escape(p.get('name', 'someone'))}</b> — "
                     f"{total} photo{'s' if total != 1 else ''} ({parts}){redo}")
    grand = sum(p.get('total', 0) for p in people)
    lines += ['', f"Total: {grand} photo{'s' if grand != 1 else ''} from "
                  f"{len(people)} {'people' if len(people) != 1 else 'person'}. Thanks all 👏"]
    return '\n'.join(lines)


def due(now: float, stats: dict) -> str | None:
    """The week to recap if a recap is due now, else None: Monday from
    RECAP_HOUR, once per week."""
    local = datetime.fromtimestamp(now, _tz())
    if local.weekday() != RECAP_WEEKDAY or local.hour < RECAP_HOUR:
        return None
    last_week = week_key(now - 7 * 86400)
    if stats.get('last_recap') == last_week:
        return None
    return last_week


def maybe_send_weekly_recap(now: float | None = None, send=None) -> bool:
    """Called by the dispatcher every tick. Sends last week's recap once, on
    Monday from RECAP_HOUR. Returns True when it sent one."""
    now = now or time.time()
    stats = load()
    if stats is None:
        return False
    week = due(now, stats)
    if week is None:
        return False
    if send is None:
        from telegram_notify import send_html as send
    if not send(recap_text(stats.get('weeks', {}).get(week, {}), week)):
        return False                     # try again on the next tick
    stats['last_recap'] = week
    save(stats)
    return True
