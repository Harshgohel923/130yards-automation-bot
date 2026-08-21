# card_batch.py — cards waiting to be posted together.
"""
The pile of hand-built cards that will go out as one Instagram post.

A single result is a single post, but a set of them — the last five matches of
a team, a night's fixtures — is a carousel, and a carousel can only be created
once every slide exists. So `/card` renders into this pile instead of posting,
and the pile is published when you say it is complete.

It lives on Cloudinary rather than in the bot's memory for the same reason the
photo flow has two entry paths: **the bot restarts often**. Its watchdog stops
it twenty minutes after the last message, and a five-card batch is easily
twenty minutes of typing. A pile held in memory would be lost by the one
interruption it most needs to survive, taking every card typed so far with it.

    manual_cards/<owner>/<seq>        the rendered card
    manual_cards/<owner>/<seq>.json   everything needed to post it later

`owner` is the Telegram user id, so two people using the same bot build
separate posts. `seq` is a zero-padded counter, so the raw listing comes back
in the order the cards were added — which is the order they appear in the
carousel, and therefore yours to choose.

The manifest carries the whole `scraper_data` dict alongside the summary
fields, so a batch that outlives the process it was typed into can still be
captioned: a single card gets the ordinary match caption, several get the
multi-match one. Nothing here needs the conversation still to exist.

Unlike carousel.py — the same idea for scraped matches — there is no deadline
and no POSTED marker. That store has to guess when a group is complete because
no human is watching; this one is told.
"""

import io
import json
import os
from datetime import datetime, timezone

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
from dotenv import load_dotenv

# Reuse the caption module's goal summariser so a manual card describes itself
# to Gemini exactly the way a scraped one does.
from caption import _summarise_events
from instagram import CAROUSEL_MAX_ITEMS

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

FOLDER = 'manual_cards'

# Instagram's own ceiling. Offering an eleventh card would mean discovering at
# post time that the whole batch is unpostable.
MAX_CARDS = CAROUSEL_MAX_ITEMS


# ── Cloudinary paths ──────────────────────────────────────────────────────────

def _prefix(owner: str) -> str:
    return f'{FOLDER}/{owner}/'


def _slide_id(owner: str, seq: int) -> str:
    return f'{_prefix(owner)}{seq:03d}'


def _manifest_id(owner: str, seq: int) -> str:
    return f'{_slide_id(owner, seq)}.json'


def _upload_json(public_id: str, payload: dict) -> str:
    """Store a dict as a raw Cloudinary resource. Returns its secure URL."""
    blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
    result = cloudinary.uploader.upload(
        blob, resource_type='raw', public_id=public_id,
        overwrite=True, invalidate=True,
    )
    return result['secure_url']


def _fetch_json(url: str) -> dict | None:
    """Read a raw JSON resource back. None when it can't be read."""
    try:
        # Cache-buster: a manifest written seconds ago must not be masked by a
        # CDN copy of the 404 that preceded it.
        response = requests.get(url, params={'_': int(datetime.now().timestamp())},
                                timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f'[batch] Could not read {url}: {e}')
        return None


# ── The pile ──────────────────────────────────────────────────────────────────

def build_manifest(scraper_data: dict, event_type: str, competition: str,
                   seq: int, image_url: str, public_id: str) -> dict:
    """Everything needed to post this card after the conversation is gone.

    The summary fields are what caption.generate_group_caption() reads; the
    scraper_data underneath is what generate_caption() needs when the batch
    turns out to hold only this one card.
    """
    sample = scraper_data.get('matchSample', {})
    if event_type.upper() == 'HT':
        home_score = str(sample.get('hts_A') or '0')
        away_score = str(sample.get('hts_B') or '0')
        ending = 'half-time'
    else:
        home_score = str(sample.get('fs_A') or '0')
        away_score = str(sample.get('fs_B') or '0')
        ending = 'full-time'

    return {
        'seq':          seq,
        'match_id':     sample.get('match_id'),
        'home_team':    sample.get('team_A_name'),
        'away_team':    sample.get('team_B_name'),
        'competition':  competition or sample.get('competition_name'),
        'home_score':   home_score,
        'away_score':   away_score,
        'penalties':    '',
        'ending':       ending,
        'goals':        _summarise_events(scraper_data.get('events', [])),
        'records':      [],
        'event_type':   event_type.upper(),
        'card_date':    sample.get('card_date'),
        'image_url':    image_url,
        'public_id':    public_id,
        'scraper_data': scraper_data,
        'added_at':     datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def pending(owner: str) -> list[dict]:
    """Cards waiting to be posted, in the order they were added.

    An empty list on a Cloudinary failure, deliberately: the caller uses this
    to decide what to *offer*, and an outage that made the bot claim a batch
    exists — or silently swallow one into a post — would be worse than one that
    makes it look empty and leaves the files untouched.
    """
    try:
        listing = cloudinary.api.resources(
            type='upload', resource_type='raw',
            prefix=_prefix(owner), max_results=100,
        )
    except Exception as e:
        print(f'[batch] Could not list cards for {owner}: {e}')
        return []

    cards = []
    for resource in listing.get('resources', []):
        manifest = _fetch_json(resource.get('secure_url', ''))
        if manifest and manifest.get('image_url'):
            cards.append(manifest)
    cards.sort(key=lambda m: m.get('seq', 0))
    return cards


def add(owner: str, image_path: str, scraper_data: dict, event_type: str,
        competition: str) -> dict:
    """Upload a rendered card and add it to this owner's pile.

    Returns the manifest. Raises on failure — the caller must be able to say
    the card was *not* kept, because the alternative is a batch that silently
    posts four of the five matches someone typed in.
    """
    seq = len(pending(owner)) + 1
    public_id = _slide_id(owner, seq)

    result = cloudinary.uploader.upload(image_path, public_id=public_id,
                                        overwrite=True, invalidate=True)
    # Cloudinary appends the format to an image public id, so record what it
    # actually stored rather than what we asked for.
    manifest = build_manifest(scraper_data, event_type, competition, seq,
                              result['secure_url'],
                              result.get('public_id', public_id))
    _upload_json(_manifest_id(owner, seq), manifest)
    print(f'[batch] {owner}: card {seq} added → {result["secure_url"]}')
    return manifest


def clear(owner: str) -> int:
    """Delete every card in this owner's pile. Returns how many were removed.

    Best-effort per resource type: a leftover image with no manifest is
    invisible to pending() and costs nothing but storage, whereas refusing to
    clear would leave the next post carrying cards from the last one.
    """
    cards = pending(owner)
    for resource_type in ('image', 'raw'):
        try:
            cloudinary.api.delete_resources_by_prefix(
                _prefix(owner), resource_type=resource_type)
        except Exception as e:
            print(f'[batch] Could not clear {resource_type} for {owner}: {e}')
    print(f'[batch] {owner}: cleared {len(cards)} card(s)')
    return len(cards)


def describe(cards: list[dict]) -> str:
    """'1. Arsenal 3–1 Man City · 21 AUG 2026' per line, for the chat."""
    return '\n'.join(
        f"{i}. {c.get('home_team', '?')} {c.get('home_score', '?')}–"
        f"{c.get('away_score', '?')} {c.get('away_team', '?')}"
        + (f" · {c['card_date']}" if c.get('card_date') else '')
        for i, c in enumerate(cards, 1)
    )
