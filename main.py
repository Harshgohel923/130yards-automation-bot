# main.py
"""
Bot orchestrator.

Match registry lives in matches.json — edit it freely while the bot is running.
The scheduler re-reads it every REGISTRY_POLL_SECS seconds so new entries are
picked up automatically without a restart.

Per-match flow
──────────────
  scheduled  →  (kickoff window opens)
  →  live      →  poll the scraper every POLL_INTERVAL_SECS; derive_status()
                  turns each scrape into a match phase
  →  ht        →  generate + post HT scorecard, keep polling
  →  ft        →  generate + post FT scorecard, worker exits

Concurrency: one daemon thread per active match via ThreadPoolExecutor.
State is kept in MATCH_STATE (in-memory dict) and flushed to state.json on
every change so a restart can resume cleanly.
"""

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from allfootball_desktop import enrich as enrich_with_desktop
from caption import generate_caption, generate_lineup_caption
from config import get_crest_url
from carousel import nudge_dispatcher, submit_match
from cloudinary_upload import upload_image, upload_match_data, delete_image
from database import init_db, is_event_posted, mark_event_posted, upsert_match
from football_scraper_dom import get_match_data
from cloudinary_utils import fetch_match_photo, match_photo_exists
from lineup_card import generate_lineup_card, has_lineups, has_positions
from instagram import (post_to_instagram, post_carousel_to_instagram,
                       delete_instagram_post, get_post_permalink)
from telegram_notify import send_alert as _send_alert_raw, send_music_reminder
from overlay_scorebar import generate_overlay_scorecard
from scorecard import generate_scorecard
from stats_card import generate_stats_card
import matchlog

load_dotenv()


# Every alert is also a log entry. Wrapping the import rather than editing
# twenty call sites means an alert added later is recorded automatically —
# what you were told and what was recorded can't drift apart.
_ALERT_LEVEL = {'❌': matchlog.ERROR, '⚠️': matchlog.WARN, '🙋': matchlog.WARN}


def send_alert(text: str, key: str | None = None, cooldown: int = 0) -> None:
    matchlog.log(
        'alert sent',
        ' '.join(str(text).split())[:200],
        level=_ALERT_LEVEL.get(str(text)[:2].strip(), matchlog.INFO),
    )
    _send_alert_raw(text, key=key, cooldown=cooldown)

# ── Tunables ──────────────────────────────────────────────────────────────────
REGISTRY_FILE       = 'matches.json'
STATE_FILE          = 'state.json'
POLL_INTERVAL_SECS  = 60       # how often to hit the scraper during a live match
REGISTRY_POLL_SECS  = 300      # how often to re-read matches.json for new entries
# Start monitoring this many seconds before kickoff
PRE_MATCH_WINDOW    = 5 * 60
# Fixtures posting a starting XI need the worker up when the lineups drop,
# around an hour before kickoff — mirrors the dispatcher's early window.
LINEUP_PRE_MATCH_WINDOW = 75 * 60
# Stop polling this many seconds after scheduled kickoff (safety ceiling; FT
# detection will stop it sooner in practice — 290 min matches the Actions job
# ceiling in match_bot.yml, and covers ET + full penalty shootout with slack for
# long stoppages and delayed kickoffs)
MAX_MATCH_DURATION  = 290 * 60
# Once the scraper has been unreachable for this long AND the match should be
# over by the clock, fall back to the last cached scrape to post FT rather than
# letting an outage swallow the post entirely.
SCRAPER_STALE_SECS  = 15 * 60
# ── Starting XI post ─────────────────────────────────────────────────────────
# Lineups are announced around an hour before kickoff, and a post_lineups
# fixture's worker starts 60-75 minutes before, so the card normally goes out
# roughly an hour ahead of the match — within a poll of the feed publishing.
# The feed publishes the names first and the grid positions a few minutes
# later; the page can draw either, so the wait for positions is bounded rather
# than open-ended.
LINEUP_SHEET_FALLBACK_SECS = 5 * 60    # before kickoff, stop holding out for the grid
LINEUP_DEADLINE_SECS       = 5 * 60    # after kickoff, a starting XI post is stale

# ── Global state ──────────────────────────────────────────────────────────────
# { match_id: { "status": str, "ht_posted": bool, "ft_posted": bool,
#               "worker_running": bool } }
MATCH_STATE: dict[str, dict] = {}
STATE_LOCK  = threading.Lock()

# Tracks which match_ids already have a worker thread spawned
ACTIVE_WORKERS: set[str] = set()
WORKERS_LOCK   = threading.Lock()

executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix='match')


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state():
    """Write MATCH_STATE to disk (called inside STATE_LOCK)."""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(MATCH_STATE, f, indent=2)


def _load_state():
    """Restore MATCH_STATE from disk on startup."""
    global MATCH_STATE
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            loaded = json.load(f)
        with STATE_LOCK:
            MATCH_STATE.update(loaded)
        print(f"[startup] Restored state for {len(loaded)} match(es) from {STATE_FILE}")
    except Exception as e:
        print(f"[startup] Could not load state file: {e}")
        send_alert(
            f"⚠️ The bot restarted and lost track of what it had already posted.\n\n"
            f"If a match was in progress at the time, its scorecard could go up "
            f"a second time — worth a quick look at the page.\n\n"
            f"Technical detail: couldn't read {STATE_FILE} — {e}"
        )


# ── Registry reader ───────────────────────────────────────────────────────────

def load_registry() -> list[dict]:
    """Read and return matches.json; returns [] on any error."""
    try:
        with open(REGISTRY_FILE, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[registry] {REGISTRY_FILE} not found — nothing to do.")
        return []
    except json.JSONDecodeError as e:
        print(f"[registry] JSON parse error in {REGISTRY_FILE}: {e}")
        send_alert(
            f"❌ The fixture list has a formatting error, so the bot can't read "
            f"any matches at all.\n\n"
            f"Nothing will post — for any game — until it's fixed. This one "
            f"needs a developer.\n\n"
            f"Technical detail: {REGISTRY_FILE} is not valid JSON — {e}",
            key='registry:parse', cooldown=1800,
        )
        return []


# ── Match phase derivation ────────────────────────────────────────────────────
# The scraper is the single source of truth for match phase. It reports
# status ('Fixture' / 'Playing' / 'Played') plus the live minute, which
# together pin down the phase more precisely — and far more reliably — than
# any external status feed. Phase names are kept short ('1H', 'HT', '2H',
# 'ET', 'AP', 'FT', 'NS') because every posting gate downstream reads them.

def _has_penalties(match_sample: dict) -> bool:
    """True once a penalty shootout scoreline exists on the scoreboard."""
    return bool(str(match_sample.get('ps_A') or '').strip()
                and str(match_sample.get('ps_B') or '').strip())


def _at_half_time(scraper_data: dict) -> bool:
    """
    True when the interval has actually started, as opposed to the clock
    sitting on 45 through first-half stoppage time.

    Two independent signals, either of which is sufficient:
      * a 'half_time' entry in the event timeline — the scraper's own explicit
        interval marker, and the more direct of the two;
      * the half-time scoreline, which is only filled in at the whistle.

    Callers must already have established that the minute is 45. Both signals
    persist for the rest of the match, so on their own they would read as HT
    long after the interval ended.
    """
    events = scraper_data.get('events')
    if isinstance(events, list):
        if any(e.get('type') == 'half_time' for e in events):
            return True
    ms = scraper_data.get('matchSample', {})
    return (str(ms.get('hts_A') or '').strip() != ''
            and str(ms.get('hts_B') or '').strip() != '')


def derive_status(scraper_data: dict) -> str:
    """
    Map the scraper's status + minute onto a match phase.

    'Played' is the scraper's own end-of-match flag and always wins. While
    'Playing', the minute decides — but it does NOT count stoppage time: it
    sits at 45 through first-half injury time and the interval alike, and at
    90 through second-half injury time. So the minute alone cannot separate
    'first-half stoppage' from 'half-time'; see _at_half_time for what does.

    A shootout scoreline overrides the minute, since the clock stops at 120
    once penalties begin.
    """
    ms     = scraper_data.get('matchSample', {})
    status = (scraper_data.get('status') or ms.get('status') or '').strip()

    if status == 'Played':
        return 'FT'
    if status != 'Playing':
        return 'NS'

    if _has_penalties(ms):
        return 'AP'

    minute = _get_minute(scraper_data)
    if minute > 90:
        return 'ET'
    if minute == 45:
        return 'HT' if _at_half_time(scraper_data) else '1H'
    return '2H' if minute > 45 else '1H'


# ── Match data helpers ────────────────────────────────────────────────────────

def _data_path(entry: dict) -> str:
    home = entry['home_team'].replace(' ', '-')
    away = entry['away_team'].replace(' ', '-')
    return os.path.join('data', f"{entry['match_id']}-{home}-vs-{away}.json")


def _save_match_data(entry: dict, scraper_data: dict):
    path = _data_path(entry)
    os.makedirs('data', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(scraper_data, f, indent=2)
    print(f"[{entry['match_id']}] Match data cached → {path}")


def _load_cached_data(entry: dict) -> dict | None:
    """Last scrape saved to disk, or None if there is none / it is unreadable."""
    path = _data_path(entry)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[{entry['match_id']}] Could not read cached match data: {e}")
        return None


def _archive_match_data(entry: dict):
    """Upload the local data JSON to Cloudinary then delete it locally."""
    path = _data_path(entry)
    if not os.path.exists(path):
        return
    try:
        upload_match_data(path)
        os.remove(path)
        print(f"[{entry['match_id']}] Match data archived to Cloudinary and removed locally.")
    except Exception as e:
        print(f"[{entry['match_id']}] Warning: could not archive match data: {e}")
        send_alert(
            f"⚠️ {_label(entry)}: the match's data file couldn't be backed up.\n\n"
            f"Nothing to worry about — the scorecard is unaffected and already "
            f"posted. It only means we don't have a saved copy of this match's "
            f"raw data.\n\n"
            f"Technical detail: upload of {path} failed — {e}"
        )


# ── Scorecard pipeline ────────────────────────────────────────────────────────

def _label(entry: dict) -> str:
    """
    'Liverpool vs Leeds United' — how a person refers to a match.

    Alerts go to people who don't read the code, so they lead with this rather
    than a match id.
    """
    return f"{entry.get('home_team', '?')} vs {entry.get('away_team', '?')}"


def _moment(event_type: str) -> str:
    """'HT' / 'FT' spelled out for an alert."""
    return {'HT': 'half-time', 'FT': 'full-time'}.get(event_type, event_type)


def _competition_of(entry: dict, scraper_data: dict) -> str | None:
    """
    Competition for this match: matches.json is authoritative, the scraper
    fills in when the field is absent. Drives the competition logo, the
    template choice and the first hashtag.
    """
    return (entry.get('competition')
            or scraper_data.get('matchSample', {}).get('competition_name'))


def _generate_card(entry: dict, event_type: str, scraper_data: dict) -> str:
    """
    Build the scorecard image, preferring the photo-overlay style when a match
    photo has been uploaded via the Telegram bot; falls back to the classic
    template scorecard when there is no photo (or the overlay render fails).
    Returns the local image path.
    """
    match_id = entry['match_id']
    competition = _competition_of(entry, scraper_data)
    photo_path = fetch_match_photo(match_id, event_type)
    if photo_path:
        try:
            path = generate_overlay_scorecard(scraper_data, photo_path,
                                              event_type=event_type,
                                              match_id_override=match_id,
                                              home_name=entry['home_team'],
                                              away_name=entry['away_team'],
                                              competition=competition)
            print(f"[{match_id}] Using photo-overlay scorecard ({event_type}).")
            matchlog.log(f'card {event_type}', 'photo overlay — your photo was used')
            return path
        except Exception as e:
            print(f"[{match_id}] Overlay scorecard failed ({e}) — falling back to template.")
            matchlog.warn(f'card {event_type}',
                          f'photo found but overlay failed, using template — {e}')
            send_alert(
                f"⚠️ {_label(entry)}: the photo you sent couldn't be used for "
                f"the {_moment(event_type)} scorecard.\n\n"
                f"The standard card is going up instead, so the post still "
                f"happens — it just won't have your photo on it. Worth trying a "
                f"different photo next time.\n\n"
                f"Technical detail: {e}"
            )
    if not photo_path:
        matchlog.log(f'card {event_type}', 'template — no photo had been uploaded')
    return generate_scorecard(scraper_data, event_type=event_type,
                              match_id_override=match_id,
                              home_name=entry['home_team'],
                              away_name=entry['away_team'],
                              competition=competition)


def _carousel_group_of(entry: dict) -> str | None:
    """The group this match posts with, or None when it posts on its own."""
    group = str(entry.get('carousel_group') or '').strip()
    return group or None


def _wants_stats_page(entry: dict, event_type: str) -> bool:
    """
    True when this post should carry a statistics slide.

    Full time only — a stats page at half time would be half a story — and
    never for a grouped match, where the carousel is one scorecard per match.
    """
    return (event_type == 'FT'
            and bool(entry.get('post_ft_stats'))
            and _carousel_group_of(entry) is None)


def _generate_slides(entry: dict, event_type: str, scraper_data: dict) -> list[str]:
    """
    Local image paths for this post, in slide order.

    Always starts with the scorecard. A match with post_ft_stats adds the
    statistics page behind it; if that page can't be built the post still goes
    out as a single card, because a missing second slide is worth far less than
    a missing result.
    """
    paths = [_generate_card(entry, event_type, scraper_data)]
    if not _wants_stats_page(entry, event_type):
        return paths

    match_id = entry['match_id']
    try:
        stats_path = generate_stats_card(
            scraper_data,
            match_id_override=match_id,
            home_name=entry['home_team'],
            away_name=entry['away_team'],
            competition=_competition_of(entry, scraper_data))
    except Exception as e:
        print(f"[{match_id}] Stats page failed to render ({e}) — posting the card alone.")
        send_alert(
            f"⚠️ {_label(entry)}: the match stats page couldn't be made.\n\n"
            f"The scorecard is posting on its own, so the result still goes up "
            f"— it's just missing the second slide.\n\n"
            f"Technical detail: {e}",
            key=f'{match_id}:stats-render', cooldown=3600,
        )
        return paths

    if stats_path:
        paths.append(stats_path)
        matchlog.log('stats page', 'rendered as slide 2')
    else:
        print(f"[{match_id}] No statistics published — posting the card alone.")
        send_alert(
            f"⚠️ {_label(entry)}: no match stats were published for this game, "
            f"so there's no stats slide.\n\n"
            f"The scorecard is posting on its own. Nothing is broken — smaller "
            f"matches often don't have stats available.",
            key=f'{match_id}:stats-missing', cooldown=3600,
        )
    return paths


def _post_slides(entry: dict, event_type: str, scraper_data: dict) -> tuple[list[str], str]:
    """
    Render → upload → caption → post, for one or several slides.

    Returns (cloudinary_public_ids, instagram_media_id). A single slide posts as
    an ordinary image; two or more post as a carousel. Raises on failure so both
    callers keep their own error handling.
    """
    paths = _generate_slides(entry, event_type, scraper_data)

    public_urls, public_ids = [], []
    for path in paths:
        url, public_id = upload_image(path)
        os.remove(path)
        public_urls.append(url)
        public_ids.append(public_id)

    caption = generate_caption(scraper_data, event_type=event_type,
                               records=entry.get('records'),
                               home_name=entry['home_team'],
                               away_name=entry['away_team'],
                               competition=_competition_of(entry, scraper_data))

    if len(public_urls) > 1:
        ig_id = post_carousel_to_instagram(public_urls, caption)
    else:
        ig_id = post_to_instagram(public_urls[0], caption)
    return public_ids, ig_id


def _run_group_pipeline(entry: dict, scraper_data: dict) -> bool:
    """
    Full time for a match that belongs to a carousel group.

    Nothing is posted here. The card goes to Cloudinary with a manifest beside
    it, and the dispatcher publishes the whole group once every member has
    landed — see carousel.py for why the publisher isn't the last worker.

    Returns True when the match was handed over, False on failure (the worker
    retries on its next poll, exactly as a failed post would).
    """
    match_id = entry['match_id']
    group = _carousel_group_of(entry)
    print(f"[{match_id}] Full time — handing over to carousel group '{group}'…")

    try:
        image_path = _generate_card(entry, 'FT', scraper_data)
        try:
            submit_match(entry, scraper_data, image_path)
        finally:
            if os.path.exists(image_path):
                os.remove(image_path)
    except Exception as e:
        print(f"[{match_id}] ❌ Carousel handover failed: {e}")
        matchlog.error(f"handover to '{group}' failed", f'{type(e).__name__}: {e}')
        send_alert(
            f"❌ {_label(entry)}: couldn't add this match to the '{group}' "
            f"carousel.\n\n"
            f"The whole carousel is on hold until it goes in — none of those "
            f"matches will post yet. The bot keeps retrying by itself every "
            f"minute.\n\n"
            f"Technical detail: {e}",
            key=f'{match_id}:carousel-submit', cooldown=600,
        )
        return False

    mark_event_posted(match_id, 'FT')
    print(f"[{match_id}] ✅ Added to carousel group '{group}'.")
    matchlog.log(f"handed to carousel '{group}'",
                 'card is staged — the group posts once every match is in',
                 force=True)
    # Let the dispatcher know now; if the nudge fails it picks the group up on
    # its next scheduled run anyway.
    nudge_dispatcher()
    return True


def _run_pipeline(entry: dict, event_type: str, scraper_data: dict):
    """
    Shared pipeline for both HT and FT:
      scraper_data → scorecard image → Cloudinary → caption → Instagram → DB
    event_type: 'HT' or 'FT'
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running {event_type} pipeline…")

    try:
        _, ig_id = _post_slides(entry, event_type, scraper_data)
        mark_event_posted(match_id, event_type)
        print(f"[{match_id}] ✅ {event_type} posted — IG ID: {ig_id}")
        matchlog.log(f'posted {event_type}', f'IG {ig_id}', force=True)
        send_music_reminder(f"{_label(entry)} — {_moment(event_type)}", ig_id)
    except Exception as e:
        print(f"[{match_id}] ❌ Pipeline error ({event_type}): {e}")
        matchlog.error(f'{event_type} post failed', f'{type(e).__name__}: {e}')
        # Do NOT mark as posted — will retry on next poll if status is unchanged
        send_alert(
            f"❌ {_label(entry)}: the {_moment(event_type)} scorecard did not "
            f"post.\n\n"
            f"It is NOT on Instagram. The bot tries again by itself every "
            f"minute while the match stays at this stage.\n\n"
            f"Technical detail: {e}",
            key=f'{match_id}:pipeline:{event_type}', cooldown=600,
        )


# ── Starting XI pipeline ──────────────────────────────────────────────────────

def _lineup_order(entry: dict) -> tuple[str, str]:
    """
    Which team leads the carousel, from the fixture's `lineups_first`.

    Defaults to the home side; anything unrecognised is treated as home rather
    than failing a post over a typo in the registry.
    """
    first = str(entry.get('lineups_first') or 'home').strip().lower()
    return ('away', 'home') if first == 'away' else ('home', 'away')


def _lineup_coach(entry: dict, side: str) -> str | None:
    """
    This side's manager, from the fixture's `coaches` entry.

    Hand-written like `records`, because no feed carries one: neither the
    mobile payload's formation block nor the desktop page has a manager field.
    Absent, the card simply leaves the line off.
    """
    coaches = entry.get('coaches')
    if not isinstance(coaches, dict):
        return None
    return str(coaches.get(side) or '').strip() or None


def _lineup_ready(entry: dict, scraper_data: dict, now) -> bool:
    """
    Is there enough team news to post?

    Positions are what makes the pitch, so they are worth waiting for — but not
    past LINEUP_SHEET_FALLBACK_SECS before kickoff, after which the names alone
    are posted as a team sheet. Waiting longer would trade a good card for no
    card at all.
    """
    if not has_lineups(scraper_data):
        return False
    if has_positions(scraper_data):
        return True
    kickoff = datetime.fromisoformat(entry['kickoff_utc'].replace('Z', '+00:00'))
    late = now >= kickoff - timedelta(seconds=LINEUP_SHEET_FALLBACK_SECS)
    if late:
        print(f"[{entry['match_id']}] Lineups published without positions — "
              f"posting them as a team sheet.")
    return late


def _run_lineup_pipeline(entry: dict, scraper_data: dict) -> bool:
    """
    The pre-match starting XI carousel: one slide per team, home first unless
    the fixture says otherwise.

    Returns True once it is on Instagram. A failure returns False and is
    retried on the next poll — right up to the deadline, after which the
    match's own coverage carries on untouched. Nothing here can affect the HT
    or FT posts: those read their own state and are never gated on this.
    """
    match_id = entry['match_id']
    first, second = _lineup_order(entry)
    print(f"[{match_id}] Posting starting XIs ({first} first)…")

    paths, public_ids, public_urls = [], [], []
    try:
        for side in (first, second):
            path = generate_lineup_card(
                scraper_data, side=side,
                match_id_override=match_id,
                home_name=entry['home_team'],
                away_name=entry['away_team'],
                competition=_competition_of(entry, scraper_data),
                coach=_lineup_coach(entry, side))
            if not path:
                raise RuntimeError(f'no {side} XI to draw')
            paths.append(path)

        for path in paths:
            url, public_id = upload_image(path)
            public_urls.append(url)
            public_ids.append(public_id)

        caption = generate_lineup_caption(
            scraper_data,
            home_name=entry['home_team'],
            away_name=entry['away_team'],
            competition=_competition_of(entry, scraper_data),
            records=entry.get('records'),
            first=first)
        ig_id = post_carousel_to_instagram(public_urls, caption)
    except Exception as e:
        print(f"[{match_id}] ❌ Starting XI post failed: {e}")
        matchlog.error('lineup post failed', f'{type(e).__name__}: {e}')
        send_alert(
            f"⚠️ {_label(entry)}: the starting XI post didn't go up.\n\n"
            f"Only the line-ups are affected — the half-time and full-time "
            f"scorecards are untouched and will post as normal. The bot keeps "
            f"trying until kickoff.\n\n"
            f"Technical detail: {e}",
            key=f'{match_id}:lineups', cooldown=600,
        )
        return False
    finally:
        for path in paths:
            if os.path.exists(path):
                os.remove(path)

    mark_event_posted(match_id, 'LINEUPS')
    print(f"[{match_id}] ✅ Starting XIs posted — IG ID: {ig_id}")
    matchlog.log('posted line-ups', f'IG {ig_id} — {first} first', force=True)
    return True


# ── Photo reminders ───────────────────────────────────────────────────────────
# When to ask for the background photo, in scraper minutes. Early enough to
# leave time to pick one and send it, late enough that the match has produced
# something worth photographing.
PHOTO_REMINDER_HT_MINUTE = 35    # first half, for the half-time card
PHOTO_REMINDER_FT_MINUTE = 80    # second half, for the full-time card
PHOTO_REMINDER_ET_MINUTE = 110   # extra time, second chance at the FT card


def _photo_reminder(entry: dict, event_type: str, state_key: str,
                    opener: str | None = None) -> None:
    """
    Ask for the match photo, once per worker run, and only when none is there.

    The flag is set before the check, so a match is looked up at most once and
    a reminder is never repeated: this is a nudge, not a nag. Sending the photo
    afterwards works exactly as it always did — the pipeline picks up whatever
    is on Cloudinary when it renders.
    """
    match_id = entry['match_id']
    with STATE_LOCK:
        if MATCH_STATE.get(match_id, {}).get(state_key):
            return
        MATCH_STATE.setdefault(match_id, {})[state_key] = True
        _save_state()

    if match_photo_exists(match_id, event_type):
        print(f"[{match_id}] {event_type} photo already uploaded — no reminder sent.")
        matchlog.log(f'photo {event_type}', 'already uploaded — no reminder needed')
        return

    moment = _moment(event_type)
    print(f"[{match_id}] Reminding about the {moment} photo.")
    matchlog.log(f'photo {event_type}', 'none uploaded yet — reminder sent')
    send_alert(
        f"📸 {opener or f'{_label(entry)} — time to send the photo for the {moment} card.'}\n\n"
        f"Message the bot privately: /start → pick this match → "
        f"{'Half Time' if event_type == 'HT' else 'Full Time'} → send the photo.\n\n"
        f"Nothing to send? That's fine — the card just uses the standard "
        f"design instead."
    )


# ── Early-posting helpers ─────────────────────────────────────────────────────

def _get_score(scraper_data: dict) -> tuple[str, str]:
    """Return raw (fs_A, fs_B) strings — used for score-change detection."""
    ms = scraper_data.get('matchSample', {})
    return (str(ms.get('fs_A') or '0'), str(ms.get('fs_B') or '0'))


def _get_minute(scraper_data: dict) -> int:
    """Scraper minute as an int; injury-time values like '90+3' → 90."""
    raw = str(scraper_data.get('matchSample', {}).get('minute') or '0')
    try:
        return int(raw.split('+')[0])
    except ValueError:
        return 0


GOAL_EVENT_TYPES = ('goal', 'penalty_goal', 'own_goal')


def _events_match_score(scraper_data: dict) -> bool:
    """
    True when the events list accounts for every goal on the scoreboard.
    The scraper updates fs_A/fs_B before the scorer event appears (typical
    for injury-time goals), which would render a card showing the new score
    with the scorer line missing. Totals are compared across both teams
    because own goals make per-team attribution ambiguous.
    """
    ms = scraper_data.get('matchSample', {})
    try:
        total_score = int(ms.get('fs_A') or 0) + int(ms.get('fs_B') or 0)
    except (TypeError, ValueError):
        return True  # unparsable score — don't block posting on it
    events = scraper_data.get('events', [])
    if not isinstance(events, list):
        return total_score == 0
    goals = sum(1 for e in events if e.get('type') in GOAL_EVENT_TYPES)
    return goals >= total_score


# Event types the scorecard prints under a team — kept in step with
# scorecard._extract_scorer_lines. Goals move the score, but a red card or a
# missed penalty changes the card without touching it, so the score alone can't
# tell whether a card that is already live has gone stale.
CARD_EVENT_TYPES = ('goal', 'penalty_goal', 'own_goal',
                    'red_card', 'penalty_missed')


def _card_events(scraper_data: dict) -> list[str]:
    """
    The displayed events as stable keys, sorted so two polls compare directly.

    Only fields the mobile scraper reports are used. The desktop enrichment's
    stoppage offset (minute_extra) is deliberately left out: it is best-effort,
    so it can appear and vanish between polls, and a card must never be
    reposted over a detail that flapped.
    """
    events = scraper_data.get('events', [])
    if not isinstance(events, list):
        return []
    return sorted(
        f"{ev.get('type')}|{ev.get('team')}|{ev.get('player')}|{ev.get('minute')}"
        for ev in events if ev.get('type') in CARD_EVENT_TYPES
    )


def _early_card_stale(stored_score, stored_events,
                      current_score, current_events) -> str | None:
    """
    Why a live early card no longer matches the match, or None if it still does.

    The string is for the log and the return value is the decision, so both
    reasons read the same way at the call site.

    stored_events is None for an early post made before this state key existed
    (a worker resumed mid-match across the upgrade). Only the score is compared
    then — the old behaviour, rather than a repost triggered by a baseline that
    was never recorded.
    """
    if stored_score and tuple(current_score) != tuple(stored_score):
        return (f'score {stored_score[0]}-{stored_score[1]}'
                f'→{current_score[0]}-{current_score[1]}')
    if stored_events is None:
        return None
    added = [e for e in current_events if e not in stored_events]
    if added:
        return 'new event: ' + ', '.join(e.replace('|', ' ') for e in added)
    # An event that only vanished is almost always the scraper dropping it for a
    # poll or two, and reposting on that would delete a correct card — and then
    # delete it again when the event came back. Nothing is done until something
    # is actually added, or the score moves.
    return None


def _wait_for_scorers(match_id: str, lagging: bool) -> bool:
    """
    Bounded wait while the scraper's events list catches up with the score.
    Returns True to skip this poll, for at most 3 consecutive polls — after
    that a permanently missing scorer entry can't block the card forever.
    """
    with STATE_LOCK:
        s = MATCH_STATE[match_id]
        if not lagging:
            s['lag_skips'] = 0
            _save_state()
            return False
        skips = s.get('lag_skips', 0)
        s['lag_skips'] = skips + 1
        _save_state()
    if skips >= 3:
        print(f"[{match_id}] Scorer list still lagging after {skips} polls — posting anyway.")
        return False
    print(f"[{match_id}] Scoreboard ahead of scorer list — waiting ({skips + 1}/3)…")
    return True


def _early_pipeline(entry: dict, event_type: str, scraper_data: dict) -> tuple[list[str] | None, str | None]:
    """
    Generate scorecard, upload to Cloudinary, post to Instagram.
    Does NOT call mark_event_posted — early posts are tracked separately.
    Returns (cloudinary_public_ids, ig_media_id), or (None, None) on failure.
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running early {event_type} pipeline…")
    try:
        cids, ig_id = _post_slides(entry, event_type, scraper_data)
        print(f"[{match_id}] ✅ Early {event_type} posted — IG ID: {ig_id}")
        matchlog.log(f'posted early {event_type}', f'IG {ig_id}', force=True)
        # Early posts are live immediately and mostly never corrected, so they
        # need their music now. A correction posts again and reminds again,
        # which is right: that is a different post.
        send_music_reminder(f"{_label(entry)} — {_moment(event_type)}", ig_id)
        return cids, ig_id
    except Exception as e:
        print(f"[{match_id}] ❌ Early {event_type} pipeline error: {e}")
        matchlog.error(f'early {event_type} post failed', f'{type(e).__name__}: {e}')
        send_alert(
            f"❌ {_label(entry)}: couldn't post the {_moment(event_type)} "
            f"scorecard early.\n\n"
            f"Nothing went out this time. The bot will try again shortly, and "
            f"again at the official whistle — so this usually sorts itself "
            f"out.\n\n"
            f"Technical detail: {e}"
        )
        return None, None


def _delete_early_post(entry: dict, event_type: str) -> None:
    """Delete the active early post from Cloudinary and Instagram, clear state."""
    match_id = entry['match_id']
    key_cid = f'early_{event_type.lower()}_cloudinary_id'
    key_ig  = f'early_{event_type.lower()}_ig_id'

    with STATE_LOCK:
        s   = MATCH_STATE.get(match_id, {})
        cid   = s.get(key_cid)
        ig_id = s.get(key_ig)

    # A stats-page post has two images behind it. Older state (and any worker
    # resuming from a state.json written before carousels) stores a bare string.
    cids = cid if isinstance(cid, list) else ([cid] if cid else [])
    for one_cid in cids:
        try:
            delete_image(one_cid)
        except Exception as e:
            print(f"[{match_id}] Warning: Cloudinary delete failed ({one_cid}): {e}")
            send_alert(
                f"⚠️ {_label(entry)}: an old copy of the scorecard image "
                f"couldn't be tidied up.\n\n"
                f"Nothing to do here — Instagram is unaffected, it just leaves "
                f"an unused file sitting in storage.\n\n"
                f"Technical detail: Cloudinary delete of {one_cid} failed — {e}"
            )

    if ig_id:
        try:
            delete_instagram_post(ig_id)
            matchlog.log('deleted superseded post', f'IG {ig_id}')
        except Exception as e:
            print(f"[{match_id}] Warning: Instagram delete failed ({ig_id}): {e}")
            matchlog.warn('delete failed — needs you',
                          f'IG {ig_id} is still up: {e}')
            link = get_post_permalink(ig_id) or f"media id {ig_id}"
            send_alert(
                f"🙋 Please delete this post manually — the bot can't:\n"
                f"{link}\n\n"
                f"{_label(entry)}: the score changed after that "
                f"{_moment(event_type)} card went up, so it now shows the wrong "
                f"result. A corrected one is being posted automatically — this "
                f"is only about removing the old one.\n\n"
                f"Instagram doesn't allow the bot to delete its own posts, so "
                f"this always needs a person."
            )

    with STATE_LOCK:
        s = MATCH_STATE.get(match_id, {})
        s[key_cid] = None
        s[key_ig]  = None
        _save_state()


# ── Per-match worker ──────────────────────────────────────────────────────────

def match_worker(entry: dict):
    """
    Runs in its own thread. Polls the scraper until FT, firing pipelines at
    HT and FT. Posts early scorecards at scraper minute 45/90 and corrects
    them if the score changes before the official whistle.
    """
    match_id    = entry['match_id']
    scraper_url = entry['scraper_url']
    is_knockout = entry.get('knockout_match', False)
    # A grouped match posts once, with its group, after the final whistle — so
    # every early-posting path is off for it. There is nothing to be early for,
    # and an early card would only have to be corrected via the broken delete.
    group       = _carousel_group_of(entry)

    print(f"[{match_id}] Worker started — {entry['home_team']} vs {entry['away_team']}"
          f"  (knockout={is_knockout}"
          f"{f', carousel={group}' if group else ''})")
    matchlog.start(entry)
    matchlog.log('settings', f"knockout={is_knockout} post_ht={entry.get('post_ht', True)} "
                             f"stats={bool(entry.get('post_ft_stats'))} "
                             f"lineups={_lineup_order(entry)[0] if entry.get('post_lineups') else 'off'} "
                             f"carousel={group or 'none'}")

    # Pre-kickoff crest check: warn now, while there is still time to fix it,
    # instead of discovering a logo-less card at HT.
    missing_crests = [t for t in (entry['home_team'], entry['away_team'])
                      if get_crest_url(t, alert=False) is None]
    matchlog.log('crests checked',
                 f"missing: {', '.join(missing_crests)}" if missing_crests else 'both found',
                 level=matchlog.WARN if missing_crests else matchlog.INFO)
    if missing_crests:
        send_alert(
            f"⚠️ {_label(entry)} kicks off shortly, but we don't have a badge "
            f"for: {', '.join(missing_crests)}.\n\n"
            f"The scorecard will still post — that team's badge will just be "
            f"blank. There's still time to fix it before half-time.\n\n"
            f"To fix: check how the team name is spelled in the fixture list "
            f"(match {match_id}), then run 'python validate_matches.py'. It "
            f"takes effect straight away — nothing needs pushing.",
            key=f'{match_id}:crest-precheck', cooldown=3600,
        )

    upsert_match(entry)

    kickoff_utc  = datetime.fromisoformat(entry['kickoff_utc'].replace('Z', '+00:00'))
    deadline_utc = kickoff_utc + timedelta(seconds=MAX_MATCH_DURATION)

    # Initialise state — setdefault preserves values from a previous run
    with STATE_LOCK:
        s = MATCH_STATE.setdefault(match_id, {})
        for k, v in {
            'status':                 'scheduled',
            'ht_posted':              False,
            'ft_posted':              False,
            'lineups_posted':         False,
            'early_ht_posted':        False,
            'early_ft_posted':        False,
            'early_ht_ig_id':         None,
            'early_ht_cloudinary_id': None,
            'early_ht_score':         None,
            'early_ht_events':        None,
            'early_ft_ig_id':         None,
            'early_ft_cloudinary_id': None,
            'early_ft_score':         None,
            'early_ft_events':        None,
            'scraper_failing_since':  None,
            'photo_reminded_ht':      False,
            'photo_reminded_ft':      False,
            'photo_reminded_et':      False,
        }.items():
            s.setdefault(k, v)
        _save_state()

    # The poll loop runs once a minute for up to three hours; only transitions
    # are worth recording, so remember what was last written to the log.
    logged_status, logged_score = None, None

    while True:
        now = datetime.now(timezone.utc)

        if now > deadline_utc:
            print(f"[{match_id}] Safety ceiling reached — stopping worker.")
            matchlog.warn('gave up', 'safety ceiling reached — match ran too long')
            break

        # ── Poll the scraper — one fetch drives the whole iteration ───────────
        scraper_data = get_match_data(scraper_url)

        if scraper_data is None:
            with STATE_LOCK:
                s = MATCH_STATE[match_id]
                if not s.get('scraper_failing_since'):
                    s['scraper_failing_since'] = now.isoformat()
                    _save_state()
                failing_since = datetime.fromisoformat(s['scraper_failing_since'])
            outage_secs = (now - failing_since).total_seconds()
            print(f"[{match_id}] Scraper fetch failed "
                  f"({outage_secs / 60:.0f} min into the outage).")
            if outage_secs < 60:
                matchlog.warn('live feed lost', 'no data from the scraper')
            send_alert(
                f"⚠️ {_label(entry)}: we've lost the live score feed for this "
                f"match.\n\n"
                f"Nothing can be posted until it comes back. The bot keeps "
                f"checking every minute and it usually recovers on its own.",
                key=f'{match_id}:scraper', cooldown=600,
            )

            # A sustained outage past the end of the match would otherwise
            # swallow the FT post entirely. Once the match cannot still be
            # running, fall back to the last good scrape so something goes out.
            latest_end = kickoff_utc + timedelta(minutes=165 if is_knockout else 115)
            cached = (_load_cached_data(entry)
                      if outage_secs >= SCRAPER_STALE_SECS and now >= latest_end
                      else None)
            if cached is None:
                time.sleep(POLL_INTERVAL_SECS)
                continue

            print(f"[{match_id}] Scraper down since {failing_since:%H:%M}Z — "
                  f"posting FT from the last cached scrape.")
            matchlog.warn('feed still down at full time',
                          f'{outage_secs / 60:.0f} min out — posting FT from the '
                          f'last score we saw')
            send_alert(
                f"⚠️ {_label(entry)}: the live score feed has been down for "
                f"{outage_secs / 60:.0f} minutes and this match should have "
                f"finished by now.\n\n"
                f"The bot is posting the full-time card using the last score it "
                f"managed to see. Please double-check the final score is right "
                f"— if a late goal went in, the card will be wrong.",
                key=f'{match_id}:stale-ft', cooldown=3600,
            )
            scraper_data = cached
            raw_status   = 'FT'
        else:
            with STATE_LOCK:
                s = MATCH_STATE[match_id]
                if s.get('scraper_failing_since'):
                    s['scraper_failing_since'] = None
                    _save_state()
            raw_status = derive_status(scraper_data)

        minute        = _get_minute(scraper_data)
        current_score = _get_score(scraper_data)
        lagging       = not _events_match_score(scraper_data)

        # ── Desktop enrichment (best-effort) ─────────────────────────────────
        # Adds stoppage-time offsets to events and the tendencies series. Only
        # fetched once a post is in prospect: it is a second request against the
        # same site, and there is nothing to enrich during a quiet first half.
        if raw_status in ('HT', 'FT', 'ET', 'AP') or minute >= 43:
            enrich_with_desktop(scraper_data, match_id)

        # What a card rendered from this poll would show, for comparison against
        # whatever a live early post is showing right now.
        current_events = _card_events(scraper_data)

        # ── Normalise status ──────────────────────────────────────────────────
        if raw_status == 'FT':
            new_status = 'ft'
        elif raw_status == 'HT':
            new_status = 'ht'
        elif raw_status in ('1H', '2H', 'ET', 'AP'):
            new_status = 'live'
        else:
            new_status = 'scheduled'

        with STATE_LOCK:
            MATCH_STATE[match_id]['status'] = new_status
            _save_state()

        # ── Snapshot mutable state for this iteration ─────────────────────────
        with STATE_LOCK:
            s               = MATCH_STATE[match_id]
            ht_posted       = s.get('ht_posted', False)
            ft_posted       = s.get('ft_posted', False)
            lineups_posted  = s.get('lineups_posted', False)
            early_ht_posted = s.get('early_ht_posted', False)
            early_ft_posted = s.get('early_ft_posted', False)
            early_ht_ig_id  = s.get('early_ht_ig_id')
            early_ft_ig_id  = s.get('early_ft_ig_id')
            early_ht_score  = tuple(s['early_ht_score']) if s.get('early_ht_score') else None
            early_ft_score  = tuple(s['early_ft_score']) if s.get('early_ft_score') else None
            early_ht_events = s.get('early_ht_events')
            early_ft_events = s.get('early_ft_events')

        print(f"[{match_id}] status={raw_status} minute={minute} score={current_score[0]}-{current_score[1]}"
              f"  ht_posted={ht_posted}  ft_posted={ft_posted}"
              f"  early_ht={early_ht_posted}  early_ft={early_ft_posted}")

        if raw_status != logged_status:
            matchlog.log(f'status {raw_status}',
                         f"minute {minute} · {current_score[0]}-{current_score[1]}")
            logged_status = raw_status
        if current_score != logged_score:
            if logged_score is not None:
                matchlog.log('score',
                             f"{logged_score[0]}-{logged_score[1]} → "
                             f"{current_score[0]}-{current_score[1]} at {minute}'")
            logged_score = current_score

        # ══════════════════════════════════════════════════════════════════════
        # STARTING XI  (pre-match, opt in with post_lineups)
        # ══════════════════════════════════════════════════════════════════════
        # Runs before anything else in the loop so the team-news post never
        # queues behind live-match work, and stops for good at kickoff +
        # LINEUP_DEADLINE_SECS: an XI graphic is worth nothing once the match
        # is under way, and retrying forever would keep alerting about it.
        if (entry.get('post_lineups') and not lineups_posted
                and not is_event_posted(match_id, 'LINEUPS')):
            if now > kickoff_utc + timedelta(seconds=LINEUP_DEADLINE_SECS):
                print(f"[{match_id}] Starting XI deadline passed — skipping the line-ups.")
                matchlog.warn('line-ups skipped',
                              'no XI was published in time — the match is under way')
                send_alert(
                    f"🙋 {_label(entry)}: no starting XI was published in time, "
                    f"so there's no team-news post for this one.\n\n"
                    f"Nothing is broken — the half-time and full-time cards are "
                    f"unaffected and will post as normal.",
                    key=f'{match_id}:lineups-missed', cooldown=3600,
                )
                with STATE_LOCK:
                    MATCH_STATE[match_id]['lineups_posted'] = True
                    _save_state()
                lineups_posted = True
            elif _lineup_ready(entry, scraper_data, now):
                if _run_lineup_pipeline(entry, scraper_data):
                    with STATE_LOCK:
                        MATCH_STATE[match_id]['lineups_posted'] = True
                        _save_state()
                    lineups_posted = True

        # ══════════════════════════════════════════════════════════════════════
        # PHOTO REMINDERS  (ask before the card is needed, not after)
        # ══════════════════════════════════════════════════════════════════════
        if (raw_status == '1H' and minute >= PHOTO_REMINDER_HT_MINUTE
                and entry.get('post_ht', True) and not ht_posted):
            _photo_reminder(entry, 'HT', 'photo_reminded_ht')
        elif raw_status == '2H' and minute >= PHOTO_REMINDER_FT_MINUTE and not ft_posted:
            _photo_reminder(entry, 'FT', 'photo_reminded_ft')
        elif raw_status == 'ET' and minute >= PHOTO_REMINDER_ET_MINUTE and not ft_posted:
            # Extra time means the FT card is still to come and the 80' nudge is
            # long past. Tracked under its own key so it fires even if that one
            # already did — but _photo_reminder still stays quiet if a photo
            # arrived in the meantime.
            _photo_reminder(
                entry, 'FT', 'photo_reminded_et',
                opener=f"{_label(entry)} has gone to extra time — last call for "
                       f"the full-time photo.")

        # ══════════════════════════════════════════════════════════════════════
        # EARLY HT MONITORING  (first half, scraper minute 45+)
        # ══════════════════════════════════════════════════════════════════════
        if (raw_status == '1H'
                and minute >= 45
                and entry.get('post_ht', True)
                and not is_event_posted(match_id, 'HT')):

            if (not early_ht_posted
                    and not _wait_for_scorers(match_id, lagging)):
                print(f"[{match_id}] Scraper minute={minute} — posting early HT scorecard…")
                cids, ig_id = _early_pipeline(entry, 'HT', scraper_data)
                if cids and ig_id:
                    with STATE_LOCK:
                        s = MATCH_STATE[match_id]
                        s['early_ht_posted']        = True
                        s['early_ht_ig_id']         = ig_id
                        s['early_ht_cloudinary_id'] = cids
                        s['early_ht_score']         = list(current_score)
                        s['early_ht_events']        = current_events
                        _save_state()

            elif early_ht_posted and early_ht_ig_id and early_ht_score:
                stale = _early_card_stale(early_ht_score, early_ht_events,
                                          current_score, current_events)
                if stale and not _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] Early HT card is out of date ({stale}) — correcting…")
                    _delete_early_post(entry, 'HT')
                    cids, ig_id = _early_pipeline(entry, 'HT', scraper_data)
                    if cids and ig_id:
                        with STATE_LOCK:
                            s = MATCH_STATE[match_id]
                            s['early_ht_ig_id']         = ig_id
                            s['early_ht_cloudinary_id'] = cids
                            s['early_ht_score']         = list(current_score)
                            s['early_ht_events']        = current_events
                            _save_state()

        # ══════════════════════════════════════════════════════════════════════
        # HT TRIGGER  (scraper reports the interval)
        # ══════════════════════════════════════════════════════════════════════
        if new_status == 'ht' and not ht_posted and not is_event_posted(match_id, 'HT'):
            with STATE_LOCK:
                active_ht_ig = MATCH_STATE[match_id].get('early_ht_ig_id')
                _eht_done    = MATCH_STATE[match_id].get('early_ht_posted', False)

            if _eht_done and active_ht_ig:
                print(f"[{match_id}] HT confirmed — early post active, skipping fallback.")
                mark_event_posted(match_id, 'HT')
                with STATE_LOCK:
                    MATCH_STATE[match_id]['ht_posted'] = True
                    _save_state()
            elif entry.get('post_ht', True):
                print(f"[{match_id}] HT confirmed — running fallback HT pipeline…")
                if _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] HT confirmed but scorer list lagging — retrying next poll.")
                else:
                    _save_match_data(entry, scraper_data)
                    _run_pipeline(entry, 'HT', scraper_data)
                    with STATE_LOCK:
                        MATCH_STATE[match_id]['ht_posted'] = True
                        _save_state()

        # ══════════════════════════════════════════════════════════════════════
        # EARLY FT MONITORING  (second half, scraper minute 90+)
        # ══════════════════════════════════════════════════════════════════════
        elif (raw_status == '2H'
                and minute >= 90
                and group is None
                and not ft_posted
                and not is_event_posted(match_id, 'FT')):

            home_s, away_s = current_score
            is_draw = (home_s == away_s)

            if not early_ft_posted:
                if is_knockout and is_draw:
                    print(f"[{match_id}] Minute={minute} — draw in knockout, waiting for injury time goal…")
                elif not _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] Minute={minute} — posting early FT scorecard…")
                    cids, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                    if cids and ig_id:
                        with STATE_LOCK:
                            s = MATCH_STATE[match_id]
                            s['early_ft_posted']        = True
                            s['early_ft_ig_id']         = ig_id
                            s['early_ft_cloudinary_id'] = cids
                            s['early_ft_score']         = list(current_score)
                            s['early_ft_events']        = current_events
                            _save_state()

            elif early_ft_ig_id and early_ft_score:
                stale = _early_card_stale(early_ft_score, early_ft_events,
                                          current_score, current_events)
                if stale:
                    if is_knockout and is_draw and current_score != early_ft_score:
                        # Equalized — delete, don't repost; ET flow takes over.
                        # No scorer-lag guard: removing a wrong card needs no events.
                        print(f"[{match_id}] Score equalized {early_ft_score}→{current_score}"
                              f" in knockout — deleting early FT post.")
                        _delete_early_post(entry, 'FT')
                        with STATE_LOCK:
                            MATCH_STATE[match_id]['early_ft_score']  = list(current_score)
                            MATCH_STATE[match_id]['early_ft_events'] = current_events
                            _save_state()
                    elif not _wait_for_scorers(match_id, lagging):
                        print(f"[{match_id}] Early FT card is out of date ({stale}) — correcting…")
                        _delete_early_post(entry, 'FT')
                        cids, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                        if cids and ig_id:
                            with STATE_LOCK:
                                s = MATCH_STATE[match_id]
                                s['early_ft_ig_id']         = ig_id
                                s['early_ft_cloudinary_id'] = cids
                                s['early_ft_score']         = list(current_score)
                                s['early_ft_events']        = current_events
                                _save_state()

        # ══════════════════════════════════════════════════════════════════════
        # FT TRIGGER  (scraper reports the match finished)
        # ══════════════════════════════════════════════════════════════════════
        elif new_status == 'ft' and not ft_posted and not is_event_posted(match_id, 'FT'):
            with STATE_LOCK:
                active_ft_ig = MATCH_STATE[match_id].get('early_ft_ig_id')
                _eft_done    = MATCH_STATE[match_id].get('early_ft_posted', False)

            if group is not None:
                # Grouped: hand the card to the carousel instead of posting it.
                if _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] FT confirmed but scorer list lagging — retrying next poll.")
                else:
                    _save_match_data(entry, scraper_data)
                    if _run_group_pipeline(entry, scraper_data):
                        with STATE_LOCK:
                            MATCH_STATE[match_id]['ft_posted'] = True
                            _save_state()
                        _archive_match_data(entry)
                        break
            elif _eft_done and active_ft_ig:
                print(f"[{match_id}] FT confirmed — early post active, skipping fallback.")
                mark_event_posted(match_id, 'FT')
                with STATE_LOCK:
                    MATCH_STATE[match_id]['ft_posted'] = True
                    _save_state()
                _archive_match_data(entry)
                break
            else:
                # Early post never made, or was deleted (e.g. equalization → ET)
                print(f"[{match_id}] FT confirmed — running fallback FT pipeline…")
                if _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] FT confirmed but scorer list lagging — retrying next poll.")
                else:
                    _save_match_data(entry, scraper_data)
                    _run_pipeline(entry, 'FT', scraper_data)
                    with STATE_LOCK:
                        MATCH_STATE[match_id]['ft_posted'] = True
                        _save_state()
                    _archive_match_data(entry)
                    break

        # ══════════════════════════════════════════════════════════════════════
        # ET / AP HANDLING  (extra time and penalty shootouts)
        # ══════════════════════════════════════════════════════════════════════
        elif (raw_status in ('ET', 'AP')
                and group is None
                and not ft_posted
                and not is_event_posted(match_id, 'FT')):
            print(f"[{match_id}] [{raw_status}] minute={minute} — evaluating…")
            home_s, away_s = current_score
            is_draw        = (home_s == away_s)
            with STATE_LOCK:
                active_ft_ig    = MATCH_STATE[match_id].get('early_ft_ig_id')
                early_ft_score  = (tuple(MATCH_STATE[match_id]['early_ft_score'])
                                   if MATCH_STATE[match_id].get('early_ft_score') else None)
                early_ft_events = MATCH_STATE[match_id].get('early_ft_events')
            et_stale = _early_card_stale(early_ft_score, early_ft_events,
                                         current_score, current_events)

            if raw_status == 'AP':
                # A shootout is under way. Any early ET card is now wrong — drop
                # it and wait; 'Played' arrives as FT and reposts with the
                # shootout result. Clearing the IG id (not the posted flag) is
                # what routes the FT trigger to a full repost.
                if active_ft_ig:
                    print(f"[{match_id}] Going to penalties — deleting ET early post…")
                    _delete_early_post(entry, 'FT')
                print(f"[{match_id}] Penalty shootout in progress — waiting for the final result…")

            elif raw_status == 'ET':
                if not is_draw:
                    if not active_ft_ig:
                        if minute < 119:
                            print(f"[{match_id}] Goal in ET score={current_score} "
                                  f"minute={minute} — waiting until 119' to post early…")
                        elif not _wait_for_scorers(match_id, lagging):
                            print(f"[{match_id}] Minute={minute}, ET score={current_score} — posting early…")
                            cids, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                            if cids and ig_id:
                                with STATE_LOCK:
                                    s = MATCH_STATE[match_id]
                                    s['early_ft_posted']        = True
                                    s['early_ft_ig_id']         = ig_id
                                    s['early_ft_cloudinary_id'] = cids
                                    s['early_ft_score']         = list(current_score)
                                    s['early_ft_events']        = current_events
                                    _save_state()
                    elif et_stale and not _wait_for_scorers(match_id, lagging):
                        print(f"[{match_id}] Early ET card is out of date ({et_stale}) — correcting…")
                        _delete_early_post(entry, 'FT')
                        cids, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                        if cids and ig_id:
                            with STATE_LOCK:
                                s = MATCH_STATE[match_id]
                                s['early_ft_ig_id']         = ig_id
                                s['early_ft_cloudinary_id'] = cids
                                s['early_ft_score']         = list(current_score)
                                s['early_ft_events']        = current_events
                                _save_state()
                else:
                    print(f"[{match_id}] ET still level at minute={minute} — waiting.")

        # Clean exit if FT was already posted in a previous run
        if new_status == 'ft' and (MATCH_STATE[match_id].get('ft_posted')
                                   or is_event_posted(match_id, 'FT')):
            break

        time.sleep(POLL_INTERVAL_SECS)

    with WORKERS_LOCK:
        ACTIVE_WORKERS.discard(match_id)
    print(f"[{match_id}] Worker exited.")
    matchlog.finish('worker exited', 'nothing further to do for this match')


def _posted_so_far(match_id: str) -> str:
    """One line naming what this worker already put on the page, for a crash alert.

    A restart cannot work this out for itself — state.json and bot.db are both
    local to the run — so the alert has to carry it while the knowledge still
    exists.
    """
    with STATE_LOCK:
        s = dict(MATCH_STATE.get(match_id, {}))
    done = [label for key, label in (
        ('lineups_posted',  'the line-ups'),
        ('early_ht_posted', 'a half-time card'),
        ('ht_posted',       'the half-time card'),
        ('early_ft_posted', 'a full-time card'),
        ('ft_posted',       'the full-time card'),
    ) if s.get(key)]
    if not done:
        return "Nothing had been posted for it yet."
    # Dedupe the early/confirmed pairs: both flags set means one card is live.
    seen, unique = set(), []
    for label in done:
        stem = label.split(' ', 1)[-1]
        if stem not in seen:
            seen.add(stem)
            unique.append(label)
    return f"Already posted: {', '.join(unique)}."


def _worker_safe(entry: dict) -> bool:
    """
    Run match_worker and alert if it crashes — a dead worker is silent.

    Returns True if the worker finished cleanly, False if it crashed, so a
    caller that owns a process (rather than a thread) can still exit non-zero
    and leave a red run behind it.
    """
    match_id = entry['match_id']
    try:
        match_worker(entry)
        return True
    except Exception as e:
        traceback.print_exc()
        with WORKERS_LOCK:
            ACTIVE_WORKERS.discard(match_id)
        matchlog.bind(match_id)
        matchlog.finish('worker crashed', traceback.format_exc().strip(),
                        level=matchlog.ERROR, match_id=match_id)
        send_alert(
            f"❌ {_label(entry)}: the bot stopped following this match "
            f"unexpectedly.\n\n"
            f"Nothing else will go out for it, and nothing will restart it on "
            f"its own — the dispatcher only starts a worker in the few minutes "
            f"around kickoff, and that window has passed.\n\n"
            f"{_posted_so_far(match_id)}\n\n"
            f"To pick it back up: run the 'Match Worker' action manually with "
            f"match id {match_id}. A restarted worker has no memory of this "
            f"run, so check the page first — anything listed above as already "
            f"posted would go up a second time.\n\n"
            f"Technical detail: {e}"
        )
        return False


# ── Registry checker (runs on APScheduler interval) ──────────────────────────

def check_registry():
    """
    Re-reads matches.json and spawns a worker thread for any match whose
    kickoff window has opened and which doesn't already have a running worker.
    """
    entries = load_registry()
    now     = datetime.now(timezone.utc)

    for entry in entries:
        match_id = entry['match_id']

        # Skip if worker already running for this match
        with WORKERS_LOCK:
            if match_id in ACTIVE_WORKERS:
                continue

        # Skip if already fully completed (FT posted)
        with STATE_LOCK:
            state = MATCH_STATE.get(match_id, {})
        if state.get('ft_posted') or is_event_posted(match_id, 'FT'):
            continue

        # Check kickoff window
        try:
            kickoff = datetime.fromisoformat(
                entry['kickoff_utc'].replace('Z', '+00:00')
            )
        except (KeyError, ValueError) as e:
            print(f"[registry] Bad kickoff_utc for match {match_id}: {e}")
            send_alert(
                f"❌ {_label(entry)}: the kick-off time in the fixture list is "
                f"missing or written incorrectly, so this match is being "
                f"skipped entirely.\n\n"
                f"No scorecards will post for it until the time is corrected "
                f"(match {match_id} in the fixture list).\n\n"
                f"Technical detail: {e}",
                key=f'registry:kickoff:{match_id}', cooldown=1800,
            )
            continue

        window_open  = kickoff - timedelta(
            seconds=LINEUP_PRE_MATCH_WINDOW if entry.get('post_lineups')
            else PRE_MATCH_WINDOW)
        window_close = kickoff + timedelta(seconds=MAX_MATCH_DURATION)

        if window_open <= now <= window_close:
            print(f"[registry] Spawning worker for match {match_id} "
                  f"({entry['home_team']} vs {entry['away_team']})")
            with WORKERS_LOCK:
                ACTIVE_WORKERS.add(match_id)
            executor.submit(_worker_safe, entry)
        elif now < window_open:
            mins = int((window_open - now).total_seconds() / 60)
            print(f"[registry] Match {match_id} starts in ~{mins} min — waiting.")
        else:
            print(f"[registry] Match {match_id} window has passed — skipping.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    init_db()
    _load_state()

    print("🤖  Scorecard bot started.")
    print(f"    Registry: {REGISTRY_FILE}  (re-read every {REGISTRY_POLL_SECS}s)")
    print(f"    Poll interval during matches: {POLL_INTERVAL_SECS}s")

    scheduler = BlockingScheduler(timezone='UTC')
    scheduler.add_job(check_registry, 'interval', seconds=REGISTRY_POLL_SECS)

    # Run immediately on startup — don't wait for first interval
    check_registry()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped.")
        executor.shutdown(wait=False)


if __name__ == '__main__':
    main()