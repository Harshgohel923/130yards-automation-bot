# carousel.py — multi-match carousel groups
"""
Posting several matches as one carousel, across separate processes.

Matches sharing a `carousel_group` id in matches.json go out as a single
Instagram post instead of one post each. The awkward part is that every match
runs in its own GitHub Actions run: separate filesystem, separate SQLite, no
shared memory. So the group needs a rendezvous point, and Cloudinary — already
holding every rendered card — is it.

    carousel/<group>/<match_id>        the scorecard image
    carousel/<group>/<match_id>.json   what that match was, for the caption
    carousel/<group>/POSTED.json       written once the group has been posted

The split of duties:

  * A worker finishing a grouped match uploads its card and manifest, then
    exits. It never posts.
  * The dispatcher publishes. It is a single serialised process that keeps
    running every 15 minutes forever, so a group cannot be stranded by a worker
    that crashed after its match — which is exactly what would happen if the
    last worker to finish were the one holding the responsibility.

If some members never arrive, publish_group() still posts what it has once
GROUP_TIMEOUT_HOURS have passed since the last kickoff in the group, and says
in a Telegram alert which matches were left out.
"""

import io
import json
import os
from datetime import datetime, timedelta, timezone

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
from dotenv import load_dotenv

from caption import generate_group_caption
# Reuse the caption module's goal summariser so a group manifest describes a
# match exactly the way a single-match prompt does.
from caption import _summarise_events
from instagram import (CAROUSEL_MAX_ITEMS, CAROUSEL_MIN_ITEMS,
                       post_carousel_to_instagram, post_to_instagram)
from telegram_notify import send_alert, send_music_reminder

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

FOLDER = 'carousel'
POSTED_MARKER = 'POSTED'

# How long after the last kickoff in a group to stop waiting for stragglers and
# post whatever has landed. Covers a full match plus extra time, penalties and
# a generous margin for a worker that had to be respawned.
GROUP_TIMEOUT_HOURS = 4


# ── Cloudinary paths ──────────────────────────────────────────────────────────

def _slide_id(group: str, match_id: str) -> str:
    return f'{FOLDER}/{group}/{match_id}'


def _manifest_id(group: str, match_id: str) -> str:
    return f'{FOLDER}/{group}/{match_id}.json'


def _posted_id(group: str) -> str:
    return f'{FOLDER}/{group}/{POSTED_MARKER}.json'


def _upload_json(public_id: str, payload: dict) -> str:
    """Store a dict as a raw Cloudinary resource. Returns its secure URL."""
    blob = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
    result = cloudinary.uploader.upload(
        blob,
        resource_type='raw',
        public_id=public_id,
        overwrite=True,
        invalidate=True,
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
        print(f'[carousel] Could not read {url}: {e}')
        return None


# ── Manifests ─────────────────────────────────────────────────────────────────

def build_manifest(entry: dict, scraper_data: dict, image_url: str,
                   public_id: str) -> dict:
    """
    Everything the publisher needs about one finished match.

    Written by the worker at full time and read by the dispatcher later, so it
    has to stand alone: the scraper data is long gone by then.
    """
    ms = scraper_data.get('matchSample', {})
    ps_home = str(ms.get('ps_A') or '').strip()
    ps_away = str(ms.get('ps_B') or '').strip()
    home_score = str(ms.get('fs_A') or '0')
    away_score = str(ms.get('fs_B') or '0')

    penalties = ''
    ending = 'full-time'
    if ps_home and ps_away:
        # Shootout goals sit in the full-time score; back them out so the
        # manifest carries the same number the scorecard shows.
        try:
            home_score = str(int(home_score) - int(ps_home))
            away_score = str(int(away_score) - int(ps_away))
        except ValueError:
            pass
        penalties = f'{ps_home}-{ps_away}'
        ending = 'penalties'
    elif str(ms.get('ets_A') or '').strip():
        ending = 'extra time'

    return {
        'match_id':    entry['match_id'],
        'home_team':   entry['home_team'],
        'away_team':   entry['away_team'],
        'competition': entry.get('competition') or ms.get('competition_name'),
        'kickoff_utc': entry.get('kickoff_utc'),
        'home_score':  home_score,
        'away_score':  away_score,
        'penalties':   penalties,
        'ending':      ending,
        'goals':       _summarise_events(scraper_data.get('events', [])),
        'records':     entry.get('records') or [],
        'image_url':   image_url,
        'public_id':   public_id,
        'written_at':  datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }


def submit_match(entry: dict, scraper_data: dict, image_path: str) -> dict:
    """
    Hand a finished grouped match over to its carousel.

    Uploads the card under a deterministic public id and writes the manifest
    beside it. Raises on failure — the worker treats that like any other failed
    pipeline and retries on its next poll.
    """
    group = str(entry['carousel_group']).strip()
    match_id = entry['match_id']
    public_id = _slide_id(group, match_id)

    result = cloudinary.uploader.upload(image_path, public_id=public_id,
                                        overwrite=True, invalidate=True)
    image_url = result['secure_url']
    # Cloudinary appends the format to an image public id, so record what it
    # actually stored rather than what we asked for.
    stored_id = result.get('public_id', public_id)
    print(f'[carousel] {group}/{match_id}: card uploaded → {image_url}')

    manifest = build_manifest(entry, scraper_data, image_url, stored_id)
    _upload_json(_manifest_id(group, match_id), manifest)
    print(f'[carousel] {group}/{match_id}: manifest written')
    return manifest


def read_manifests(group: str) -> dict[str, dict]:
    """Every manifest that has landed for a group, keyed by match_id."""
    try:
        listing = cloudinary.api.resources(
            type='upload', resource_type='raw',
            prefix=f'{FOLDER}/{group}/', max_results=100,
        )
    except Exception as e:
        print(f'[carousel] Could not list manifests for {group}: {e}')
        return {}

    manifests = {}
    for resource in listing.get('resources', []):
        public_id = resource.get('public_id', '')
        if public_id.endswith(f'{POSTED_MARKER}.json'):
            continue
        manifest = _fetch_json(resource.get('secure_url', ''))
        if manifest and manifest.get('match_id'):
            manifests[str(manifest['match_id'])] = manifest
    return manifests


def posted_marker(group: str) -> dict | None:
    """The marker written after a successful post, or None if never posted."""
    try:
        resource = cloudinary.api.resource(_posted_id(group), resource_type='raw')
    except Exception:
        return None
    return _fetch_json(resource.get('secure_url', ''))


def mark_group_posted(group: str, ig_id: str, match_ids: list[str],
                      missing: list[str]) -> None:
    _upload_json(_posted_id(group), {
        'group':      group,
        'ig_media_id': ig_id,
        'match_ids':  match_ids,
        'missing':    missing,
        'posted_at':  datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })


# ── Publishing ────────────────────────────────────────────────────────────────

def _label(entry: dict) -> str:
    """'Liverpool vs Leeds United' — how a person refers to a match."""
    return f"{entry.get('home_team', '?')} vs {entry.get('away_team', '?')}"


def _labels(entries: list[dict], match_ids: list[str]) -> str:
    """Readable names for a set of match ids, for an alert."""
    by_id = {str(e.get('match_id')): e for e in entries}
    return ', '.join(_label(by_id[i]) if i in by_id else i for i in match_ids)


def _sort_key(manifest: dict) -> tuple:
    """Carousel order: kickoff time, then match id to break ties."""
    kickoff = manifest.get('kickoff_utc') or ''
    try:
        parsed = datetime.fromisoformat(str(kickoff).replace('Z', '+00:00'))
    except ValueError:
        parsed = datetime.max.replace(tzinfo=timezone.utc)
    return (parsed, str(manifest.get('match_id', '')))


def group_deadline(entries: list[dict]) -> datetime | None:
    """When to stop waiting for missing members and post what we have."""
    kickoffs = []
    for entry in entries:
        try:
            kickoffs.append(datetime.fromisoformat(
                str(entry['kickoff_utc']).replace('Z', '+00:00')))
        except (KeyError, ValueError):
            continue
    if not kickoffs:
        return None
    return max(kickoffs) + timedelta(hours=GROUP_TIMEOUT_HOURS)


def publish_group(group: str, entries: list[dict], now: datetime | None = None) -> str | None:
    """
    Post a group's carousel if it is ready.

    Returns the Instagram media id when it posts, None when it isn't time yet
    (members still to come, and the deadline not reached) or it has already been
    posted. Raises only on an actual posting failure, which the dispatcher
    reports — the group stays unmarked and is retried on the next tick.
    """
    now = now or datetime.now(timezone.utc)

    if posted_marker(group):
        print(f'[carousel] {group}: already posted, skipping.')
        return None

    members = [str(e['match_id']) for e in entries]
    manifests = read_manifests(group)
    missing = [m for m in members if m not in manifests]

    if missing:
        deadline = group_deadline(entries)
        if deadline is None or now < deadline:
            waiting = f'{len(manifests)}/{len(members)}'
            print(f'[carousel] {group}: {waiting} matches in, waiting for {missing}.')
            return None
        if not manifests:
            print(f'[carousel] {group}: deadline passed with nothing to post.')
            send_alert(
                f"❌ The '{group}' carousel never posted — none of its "
                f"{len(members)} matches produced a scorecard in time.\n\n"
                f"Nothing went out for this group at all. Worth checking "
                f"whether these matches actually took place:\n"
                f"{_labels(entries, members)}",
                key=f'carousel:{group}:empty', cooldown=86400,
            )
            return None
        print(f'[carousel] {group}: deadline passed, posting without {missing}.')
        send_alert(
            f"⚠️ The '{group}' carousel is going out with {len(manifests)} of "
            f"its {len(members)} matches.\n\n"
            f"We waited, but no scorecard ever came through for:\n"
            f"{_labels(entries, missing)}\n\n"
            f"The rest is posting now, without them.",
            key=f'carousel:{group}:partial', cooldown=86400,
        )

    ordered = sorted(manifests.values(), key=_sort_key)

    # validate_matches.py rejects oversized groups, so this is a backstop for a
    # registry edited straight on master — better a trimmed post than a crash.
    if len(ordered) > CAROUSEL_MAX_ITEMS:
        dropped = [str(m['match_id']) for m in ordered[CAROUSEL_MAX_ITEMS:]]
        ordered = ordered[:CAROUSEL_MAX_ITEMS]
        send_alert(
            f"⚠️ The '{group}' carousel has {len(ordered) + len(dropped)} "
            f"matches, but Instagram only allows {CAROUSEL_MAX_ITEMS} in a "
            f"single post.\n\n"
            f"The first {CAROUSEL_MAX_ITEMS} are posting, earliest kick-off "
            f"first. These are left out:\n{_labels(entries, dropped)}\n\n"
            f"To avoid this, keep each group to {CAROUSEL_MAX_ITEMS} matches — "
            f"'python validate_matches.py' flags it before matchday.",
            key=f'carousel:{group}:oversized', cooldown=86400,
        )

    urls = [m['image_url'] for m in ordered if m.get('image_url')]
    caption = generate_group_caption(ordered)

    if len(urls) >= CAROUSEL_MIN_ITEMS:
        ig_id = post_carousel_to_instagram(urls, caption)
    elif urls:
        # A group that ended up with one usable card still deserves its post.
        print(f'[carousel] {group}: only one card available — posting it alone.')
        ig_id = post_to_instagram(urls[0], caption)
    else:
        raise RuntimeError(f'group {group} has manifests but no usable image urls')

    posted_ids = [str(m['match_id']) for m in ordered]
    mark_group_posted(group, ig_id, posted_ids, missing)
    print(f'[carousel] {group}: ✅ posted {len(urls)} slides — IG ID: {ig_id}')
    send_music_reminder(
        f"The '{group}' carousel — {len(urls)} "
        f"{'matches' if len(urls) != 1 else 'match'}", ig_id)
    return ig_id


def nudge_dispatcher() -> bool:
    """
    Ask the dispatcher to run now rather than at its next 15-minute tick.

    Called by a worker that has just handed its match over, so a group whose
    last match has finished posts within about a minute. Best-effort in the
    strongest sense: with no token (running locally) or on any failure the group
    still goes out, just on the dispatcher's own schedule.
    """
    token = os.getenv('GH_TOKEN') or os.getenv('GITHUB_TOKEN')
    repo = os.getenv('GITHUB_REPOSITORY')
    if not token or not repo:
        print('[carousel] No GitHub credentials — leaving the group for the '
              'dispatcher’s next scheduled run.')
        return False
    try:
        response = requests.post(
            f'https://api.github.com/repos/{repo}/actions/workflows/dispatcher.yml/dispatches',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            json={'ref': 'master'},
            timeout=10,
        )
        if response.status_code == 204:
            print('[carousel] Nudged the dispatcher.')
            return True
        print(f'[carousel] Dispatcher nudge returned {response.status_code}: '
              f'{response.text} — it will run on schedule instead.')
    except Exception as e:
        print(f'[carousel] Dispatcher nudge failed ({e}) — it will run on schedule.')
    return False


def groups_in(registry: list[dict]) -> dict[str, list[dict]]:
    """matches.json → {carousel_group: [entries]}, ungrouped matches omitted."""
    groups: dict[str, list[dict]] = {}
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        group = str(entry.get('carousel_group') or '').strip()
        if group:
            groups.setdefault(group, []).append(entry)
    return groups
