"""
Template fetcher.

Templates are stored in Cloudinary as fixed assets defined in config.py:
  CLOUDINARY_TEMPLATES = {
      "HT": "Half_time_template_ujqiub",
      "FT": "Full_time_template_hckszk",
  }

Both templates are shared across all matches — there are no per-match
template variants. Templates are cached locally after the first download
so subsequent calls within the same run are instant.
"""

import os

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
from dotenv import load_dotenv

from config import CLOUDINARY_TEMPLATES, CLOUD_NAME

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

LOCAL_CACHE_DIR = 'assets/templates'
MATCH_PHOTO_FOLDER = 'match_photos'


def photo_public_id(match_id: str, event_type: str) -> str:
    """Deterministic Cloudinary public_id for a match photo uploaded via the
    Telegram bot: match_photos/<match_id>_<HT|FT>."""
    return f"{MATCH_PHOTO_FOLDER}/{match_id}_{event_type.upper()}"


# ── Extra carousel photos ────────────────────────────────────────────────────
# Besides the background, an FT post can carry extra photos as carousel slides
# — after the scorecard, before the stats page. Sent through /start, stored
# beside the background in the order they arrive. HT posts stay one card.

# Instagram takes 10 slides; the scorecard is one and the stats page may be
# another, so eight extras always fit.
EXTRA_PHOTO_LIMIT = 8

# Every slide is shown at the first one's shape, and the scorecard is 3:4, so
# an extra is cropped to 3:4 at delivery — Cloudinary's subject-aware crop —
# and served as a phone-friendly JPEG.
EXTRA_SLIDE_TRANSFORM = 'c_fill,ar_3:4,g_auto,w_1440,q_auto,f_jpg'


def extra_photo_prefix(match_id: str, event_type: str) -> str:
    return f"{photo_public_id(match_id, event_type)}_extra_"


def extra_photo_public_id(match_id: str, event_type: str, n: int) -> str:
    """match_photos/<id>_<HT|FT>_extra_01 — zero-padded so names sort in order."""
    return f"{extra_photo_prefix(match_id, event_type)}{n:02d}"


def list_extra_photos(match_id: str, event_type: str) -> list[dict]:
    """The extras for a post, in the order they were added: [{public_id, url}].
    Raises if Cloudinary can't be asked — callers decide what that costs."""
    prefix = extra_photo_prefix(match_id, event_type)
    found = cloudinary.api.resources(type='upload', prefix=prefix, max_results=50)
    items = [{'public_id': r['public_id'], 'url': r['secure_url']}
             for r in found.get('resources', [])
             if r.get('public_id', '').startswith(prefix)]
    return sorted(items, key=lambda r: r['public_id'])


def next_extra_photo_number(existing: list[dict]) -> int:
    """One past the highest number used, so a removed middle photo never
    makes a new one overwrite the last."""
    numbers = []
    for item in existing:
        tail = item['public_id'].rsplit('_', 1)[-1]
        if tail.isdigit():
            numbers.append(int(tail))
    return max(numbers, default=0) + 1


def delete_extra_photos(match_id: str, event_type: str) -> int:
    """Remove every extra for a post. Returns how many went."""
    items = list_extra_photos(match_id, event_type)
    if items:
        cloudinary.api.delete_resources([i['public_id'] for i in items], invalidate=True)
    return len(items)


def extra_slide_url(url: str) -> str:
    """The URL Instagram is given for an extra: cropped to 3:4, as JPEG."""
    marker = '/image/upload/'
    return url.replace(marker, f'{marker}{EXTRA_SLIDE_TRANSFORM}/', 1) if marker in url else url


def match_photo_exists(match_id: str, event_type: str) -> bool:
    """
    Has a photo been uploaded for this match/event yet?

    A HEAD request, so asking doesn't pull the whole image down — this runs
    while a match is live, purely to decide whether to send a reminder.

    On a network error it answers False, i.e. "no photo". The reminder then
    goes out and the worst case is a nudge about a photo already sent; the
    alternative, staying quiet during a Cloudinary blip, loses the reminder
    for the one match that needed it.
    """
    url = (f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/"
           f"{photo_public_id(match_id, event_type)}")
    try:
        return requests.head(url, timeout=10).status_code == 200
    except requests.RequestException:
        return False


def fetch_match_photo(match_id: str, event_type: str, dest_dir: str = 'assets/match_photos') -> str | None:
    """Download the bot-uploaded photo for a match/event to a local file.
    Returns the local path, or None if no photo has been uploaded."""
    os.makedirs(dest_dir, exist_ok=True)
    local_path = os.path.join(dest_dir, f"{match_id}_{event_type.upper()}.jpg")
    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{photo_public_id(match_id, event_type)}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception:
        return None
    with open(local_path, 'wb') as f:
        f.write(response.content)
    return local_path


def fetch_template(template_key: str) -> str | None:
    """
    Download a scorecard template from Cloudinary and cache it locally.

    template_key: a key of CLOUDINARY_TEMPLATES ('UCL', 'random-3', …), as
                  chosen by config.select_template_key().

    Returns the local cached file path on success, None on failure.
    """
    public_id = CLOUDINARY_TEMPLATES.get(template_key)
    if not public_id:
        print(f"[cloudinary] No template configured for key '{template_key}'")
        return None

    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
    # The public_id is part of the cache key, so pointing CLOUDINARY_TEMPLATES
    # at a new template invalidates every stale cached copy automatically.
    safe_id = public_id.replace('/', '_')
    local_path = os.path.join(LOCAL_CACHE_DIR, f'{safe_id}.png')

    # Return cached copy if already downloaded
    if os.path.exists(local_path):
        return local_path

    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{public_id}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        with open(local_path, 'wb') as f:
            f.write(response.content)
        print(f"[cloudinary] Template downloaded: {template_key} → {local_path}")
        return local_path

    except Exception as e:
        print(f"[cloudinary] Error fetching template '{template_key}': {e}")
        return None