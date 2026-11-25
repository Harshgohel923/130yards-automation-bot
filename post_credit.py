"""
Credit for bot posts — whose photos went into what, and how those posts did.

When the bot publishes, it records who contributed, one small JSON per post
at stats/posts/<ISO week>/<media id>.json (one file each, so workers posting
side by side never overwrite one another):

  • an HT/FT card    — whoever sent the background, and any carousel extras
  • an event photo   — whoever staged it
  • a carousel group — everyone whose photos are in it
  • a /card post     — whoever posted it

The sender is read off the photo itself: every upload through the bot stores
uploaded_by / uploaded_by_id / uploaded_by_username on the Cloudinary asset.
Photos uploaded before that only carry a name, and are credited by name.

On Monday the Weekly Recap asks Instagram how last week's posts did and adds
it up per person. Likes and comments come with the posting token. Reach,
saves and shares need the instagram_manage_insights permission: they are
asked for once per recap and, if the token can't have them, left out.
"""

import io
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

LEDGER = 'stats/posts'
GRAPH_URL = 'https://graph.facebook.com/v19.0'
INSIGHT_METRICS = ('reach', 'saved', 'shares')


def _cloudinary():
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
                      api_key=os.getenv('CLOUDINARY_API_KEY'),
                      api_secret=os.getenv('CLOUDINARY_API_SECRET'))
    return cloudinary


def person_key(person: dict) -> str:
    return str(person['id']) if person.get('id') else f"name:{person.get('name', '?')}"


# ── Who sent a photo ─────────────────────────────────────────────────────────

def uploader_of(public_id: str) -> dict | None:
    """{id, name, username} from the photo's own metadata, or None."""
    cloudinary = _cloudinary()
    try:
        found = cloudinary.api.resource(public_id, context=True)
    except Exception:
        return None
    custom = (found.get('context') or {}).get('custom') or {}
    name = custom.get('uploaded_by', '')
    uid = custom.get('uploaded_by_id', '')
    if not (name or uid):
        return None
    return {'id': uid, 'name': name or 'someone',
            'username': custom.get('uploaded_by_username', '')}


def _unique(people: list) -> list[dict]:
    seen, out = set(), []
    for p in people:
        if p and person_key(p) not in seen:
            seen.add(person_key(p))
            out.append(p)
    return out


def contributors_for_card(match_id: str, moment: str) -> list[dict]:
    """Whoever sent the background (and, for FT, the carousel extras)."""
    from cloudinary_utils import list_extra_photos, photo_public_id
    ids = [photo_public_id(match_id, moment)]
    if moment == 'FT':
        try:
            ids += [x['public_id'] for x in list_extra_photos(match_id, moment)]
        except Exception:
            pass
    return _unique([uploader_of(pid) for pid in ids])


# ── The ledger ───────────────────────────────────────────────────────────────

def record_post(media_id: str, kind: str, label: str, contributors: list[dict],
                ts: float | None = None) -> bool:
    """Write the post's credit. Nothing is written when nobody contributed (a
    plain template card). Never raises: credit must not cost a post."""
    contributors = _unique(contributors)
    if not media_id or not contributors:
        return False
    import leaderboard
    ts = ts or time.time()
    record = {'media_id': str(media_id), 'kind': kind, 'label': label,
              'posted_ts': int(ts), 'contributors': contributors}
    try:
        cloudinary = _cloudinary()
        blob = io.BytesIO(json.dumps(record, ensure_ascii=False).encode())
        cloudinary.uploader.upload(
            blob, resource_type='raw', overwrite=True,
            public_id=f'{LEDGER}/{leaderboard.week_key(ts)}/{media_id}.json')
        return True
    except Exception as e:
        print(f'[post_credit] Could not record credit for {media_id}: {e}')
        return False


def load_week(week: str) -> list[dict]:
    """Every post recorded for a week."""
    cloudinary = _cloudinary()
    out, cursor = [], None
    while True:
        kw = {'type': 'upload', 'resource_type': 'raw',
              'prefix': f'{LEDGER}/{week}/', 'max_results': 500}
        if cursor:
            kw['next_cursor'] = cursor
        listing = cloudinary.api.resources(**kw)
        for r in listing.get('resources', []):
            try:
                resp = requests.get(r['secure_url'], params={'_': int(time.time())}, timeout=15)
                resp.raise_for_status()
                out.append(resp.json())
            except Exception:
                continue
        cursor = listing.get('next_cursor')
        if not cursor:
            return out


# ── How a post did ───────────────────────────────────────────────────────────

def engagement(media_id: str, want_insights: bool = True) -> tuple[dict | None, bool]:
    """
    ({likes, comments[, reach, saved, shares]}, insights_allowed) — or
    (None, …) when the post is gone (a corrected early card was deleted).
    """
    token = os.getenv('IG_ACCESS_TOKEN')
    try:
        r = requests.get(f'{GRAPH_URL}/{media_id}',
                         params={'fields': 'like_count,comments_count', 'access_token': token},
                         timeout=15)
        if r.status_code != 200:
            return None, want_insights
        data = r.json()
    except Exception:
        return None, want_insights
    stats = {'likes': data.get('like_count', 0) or 0,
             'comments': data.get('comments_count', 0) or 0}
    if want_insights:
        try:
            r = requests.get(f'{GRAPH_URL}/{media_id}/insights',
                             params={'metric': ','.join(INSIGHT_METRICS), 'access_token': token},
                             timeout=15)
            if r.status_code == 200:
                for d in r.json().get('data', []):
                    stats[d['name']] = (d.get('values') or [{}])[0].get('value', 0) or 0
            elif 'permission' in r.text.lower():
                want_insights = False        # the token can't — stop asking
        except Exception:
            pass
    return stats, want_insights


def weekly_credit(week: str) -> dict:
    """{person key: {name, username, posts, likes, comments[, reach, saved, shares]}}."""
    totals: dict = {}
    want_insights = True
    for post in load_week(week):
        stats, want_insights = engagement(post.get('media_id', ''), want_insights)
        if stats is None:
            continue
        for person in post.get('contributors', []):
            t = totals.setdefault(person_key(person), {
                'name': person.get('name', 'someone'),
                'username': person.get('username', ''), 'posts': 0})
            if person.get('username'):
                t['username'] = person['username']
            t['posts'] += 1
            for k, v in stats.items():
                t[k] = t.get(k, 0) + v
    return totals
