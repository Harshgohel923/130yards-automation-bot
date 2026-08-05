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

from caption import generate_caption
from config import get_crest_url
from cloudinary_upload import upload_image, upload_match_data, delete_image
from database import init_db, is_event_posted, mark_event_posted, upsert_match
from football_scraper_dom import get_match_data
from cloudinary_utils import fetch_match_photo
from instagram import post_to_instagram, delete_instagram_post, get_post_permalink
from telegram_notify import send_alert
from overlay_scorebar import generate_overlay_scorecard
from scorecard import generate_scorecard

load_dotenv()

# ── Tunables ──────────────────────────────────────────────────────────────────
REGISTRY_FILE       = 'matches.json'
STATE_FILE          = 'state.json'
POLL_INTERVAL_SECS  = 60       # how often to hit the scraper during a live match
REGISTRY_POLL_SECS  = 300      # how often to re-read matches.json for new entries
# Start monitoring this many seconds before kickoff
PRE_MATCH_WINDOW    = 5 * 60
# Stop polling this many seconds after scheduled kickoff (safety ceiling; FT
# detection will stop it sooner in practice — 210 min covers ET + full penalty shootout)
MAX_MATCH_DURATION  = 210 * 60
# Once the scraper has been unreachable for this long AND the match should be
# over by the clock, fall back to the last cached scrape to post FT rather than
# letting an outage swallow the post entirely.
SCRAPER_STALE_SECS  = 15 * 60

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
            f"⚠️ Bot startup: could not load {STATE_FILE} ({e}).\n"
            f"Early-post tracking from previous runs is lost — duplicate "
            f"posts are possible for matches that were mid-game."
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
            f"❌ {REGISTRY_FILE} is invalid JSON ({e}).\n"
            f"No matches can be scheduled until the file is fixed.",
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
            f"⚠️ {entry['match_id']}: couldn't archive the match data JSON to "
            f"Cloudinary ({e}).\nThe local copy is kept at {path} — upload it "
            f"manually if you want it archived."
        )


# ── Scorecard pipeline ────────────────────────────────────────────────────────

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
            return path
        except Exception as e:
            print(f"[{match_id}] Overlay scorecard failed ({e}) — falling back to template.")
            send_alert(
                f"⚠️ {match_id}: the photo-overlay {event_type} scorecard "
                f"failed to render ({e}).\nPosting the plain template card "
                f"instead — check the match photo you uploaded."
            )
    return generate_scorecard(scraper_data, event_type=event_type,
                              match_id_override=match_id,
                              home_name=entry['home_team'],
                              away_name=entry['away_team'],
                              competition=competition)


def _run_pipeline(entry: dict, event_type: str, scraper_data: dict):
    """
    Shared pipeline for both HT and FT:
      scraper_data → scorecard image → Cloudinary → caption → Instagram → DB
    event_type: 'HT' or 'FT'
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running {event_type} pipeline…")

    try:
        image_path = _generate_card(entry, event_type, scraper_data)
        public_url, _ = upload_image(image_path)
        os.remove(image_path)
        caption = generate_caption(scraper_data, event_type=event_type,
                                   records=entry.get('records'),
                                   home_name=entry['home_team'],
                                   away_name=entry['away_team'],
                                   competition=_competition_of(entry, scraper_data))
        ig_id   = post_to_instagram(public_url, caption)
        mark_event_posted(match_id, event_type)
        print(f"[{match_id}] ✅ {event_type} posted — IG ID: {ig_id}")
    except Exception as e:
        print(f"[{match_id}] ❌ Pipeline error ({event_type}): {e}")
        # Do NOT mark as posted — will retry on next poll if status is unchanged
        send_alert(
            f"❌ {match_id}: the {event_type} pipeline failed ({e}).\n"
            f"The scorecard was NOT posted. It retries automatically every "
            f"{POLL_INTERVAL_SECS}s while the match status is unchanged.",
            key=f'{match_id}:pipeline:{event_type}', cooldown=600,
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


def _early_pipeline(entry: dict, event_type: str, scraper_data: dict) -> tuple[str | None, str | None]:
    """
    Generate scorecard, upload to Cloudinary, post to Instagram.
    Does NOT call mark_event_posted — early posts are tracked separately.
    Returns (cloudinary_public_id, ig_media_id), or (None, None) on failure.
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running early {event_type} pipeline…")
    try:
        image_path = _generate_card(entry, event_type, scraper_data)
        public_url, cid = upload_image(image_path)
        os.remove(image_path)
        caption = generate_caption(scraper_data, event_type=event_type,
                                   records=entry.get('records'),
                                   home_name=entry['home_team'],
                                   away_name=entry['away_team'],
                                   competition=_competition_of(entry, scraper_data))
        ig_id = post_to_instagram(public_url, caption)
        print(f"[{match_id}] ✅ Early {event_type} posted — IG ID: {ig_id}")
        return cid, ig_id
    except Exception as e:
        print(f"[{match_id}] ❌ Early {event_type} pipeline error: {e}")
        send_alert(
            f"❌ {match_id}: the early {event_type} pipeline failed ({e}).\n"
            f"No early scorecard was posted for this attempt — the worker "
            f"will try again on a later poll or at the official whistle."
        )
        return None, None


def _delete_early_post(match_id: str, event_type: str) -> None:
    """Delete the active early post from Cloudinary and Instagram, clear state."""
    key_cid = f'early_{event_type.lower()}_cloudinary_id'
    key_ig  = f'early_{event_type.lower()}_ig_id'

    with STATE_LOCK:
        s   = MATCH_STATE.get(match_id, {})
        cid   = s.get(key_cid)
        ig_id = s.get(key_ig)

    if cid:
        try:
            delete_image(cid)
        except Exception as e:
            print(f"[{match_id}] Warning: Cloudinary delete failed ({cid}): {e}")
            send_alert(
                f"⚠️ {match_id}: couldn't delete the old scorecard image from "
                f"Cloudinary ({cid}): {e}.\nHarmless for Instagram — it just "
                f"leaves an orphaned image in the scorecards folder."
            )

    if ig_id:
        try:
            delete_instagram_post(ig_id)
        except Exception as e:
            print(f"[{match_id}] Warning: Instagram delete failed ({ig_id}): {e}")
            link = get_post_permalink(ig_id) or f"media id {ig_id}"
            send_alert(
                f"⚠️ {match_id}: couldn't delete the outdated {event_type} "
                f"scorecard — delete it manually:\n{link}\n\n"
                f"(The corrected card is being posted automatically.)"
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

    print(f"[{match_id}] Worker started — {entry['home_team']} vs {entry['away_team']}"
          f"  (knockout={is_knockout})")

    # Pre-kickoff crest check: warn now, while there is still time to fix it,
    # instead of discovering a logo-less card at HT.
    missing_crests = [t for t in (entry['home_team'], entry['away_team'])
                      if get_crest_url(t, alert=False) is None]
    if missing_crests:
        send_alert(
            f"⚠️ {match_id}: worker started for {entry['home_team']} vs "
            f"{entry['away_team']}, but no crest on Cloudinary for: "
            f"{', '.join(missing_crests)}. Scorecards will render without "
            f"the logo(s) unless fixed before HT.\n\n"
            f"Fix: check the spelling in matches.json, then run "
            f"'python validate_matches.py' locally — the upload takes effect "
            f"immediately, no push needed.",
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
            'early_ht_posted':        False,
            'early_ft_posted':        False,
            'early_ht_ig_id':         None,
            'early_ht_cloudinary_id': None,
            'early_ht_score':         None,
            'early_ft_ig_id':         None,
            'early_ft_cloudinary_id': None,
            'early_ft_score':         None,
            'scraper_failing_since':  None,
        }.items():
            s.setdefault(k, v)
        _save_state()

    while True:
        now = datetime.now(timezone.utc)

        if now > deadline_utc:
            print(f"[{match_id}] Safety ceiling reached — stopping worker.")
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
            send_alert(
                f"⚠️ {match_id}: the live-score scraper returned no data.\n"
                f"Match tracking is stalled until it recovers — retrying every "
                f"{POLL_INTERVAL_SECS}s.",
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
            send_alert(
                f"⚠️ {match_id}: the scraper has been down for "
                f"{outage_secs / 60:.0f} min and the match should be over.\n"
                f"Posting the FT card from the last cached scrape — check the "
                f"final score is correct.",
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
            early_ht_posted = s.get('early_ht_posted', False)
            early_ft_posted = s.get('early_ft_posted', False)
            early_ht_ig_id  = s.get('early_ht_ig_id')
            early_ft_ig_id  = s.get('early_ft_ig_id')
            early_ht_score  = tuple(s['early_ht_score']) if s.get('early_ht_score') else None
            early_ft_score  = tuple(s['early_ft_score']) if s.get('early_ft_score') else None

        print(f"[{match_id}] status={raw_status} minute={minute} score={current_score[0]}-{current_score[1]}"
              f"  ht_posted={ht_posted}  ft_posted={ft_posted}"
              f"  early_ht={early_ht_posted}  early_ft={early_ft_posted}")

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
                cid, ig_id = _early_pipeline(entry, 'HT', scraper_data)
                if cid and ig_id:
                    with STATE_LOCK:
                        s = MATCH_STATE[match_id]
                        s['early_ht_posted']        = True
                        s['early_ht_ig_id']         = ig_id
                        s['early_ht_cloudinary_id'] = cid
                        s['early_ht_score']         = list(current_score)
                        _save_state()

            elif early_ht_posted and early_ht_ig_id and early_ht_score:
                if (current_score != early_ht_score
                        and not _wait_for_scorers(match_id, lagging)):
                    print(f"[{match_id}] HT score changed {early_ht_score}→{current_score} — correcting…")
                    _delete_early_post(match_id, 'HT')
                    cid, ig_id = _early_pipeline(entry, 'HT', scraper_data)
                    if cid and ig_id:
                        with STATE_LOCK:
                            s = MATCH_STATE[match_id]
                            s['early_ht_ig_id']         = ig_id
                            s['early_ht_cloudinary_id'] = cid
                            s['early_ht_score']         = list(current_score)
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
                and not ft_posted
                and not is_event_posted(match_id, 'FT')):

            home_s, away_s = current_score
            is_draw = (home_s == away_s)

            if not early_ft_posted:
                if is_knockout and is_draw:
                    print(f"[{match_id}] Minute={minute} — draw in knockout, waiting for injury time goal…")
                elif not _wait_for_scorers(match_id, lagging):
                    print(f"[{match_id}] Minute={minute} — posting early FT scorecard…")
                    cid, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                    if cid and ig_id:
                        with STATE_LOCK:
                            s = MATCH_STATE[match_id]
                            s['early_ft_posted']        = True
                            s['early_ft_ig_id']         = ig_id
                            s['early_ft_cloudinary_id'] = cid
                            s['early_ft_score']         = list(current_score)
                            _save_state()

            elif early_ft_ig_id and early_ft_score:
                if current_score != early_ft_score:
                    if is_knockout and is_draw:
                        # Equalized — delete, don't repost; ET flow takes over.
                        # No scorer-lag guard: removing a wrong card needs no events.
                        print(f"[{match_id}] Score equalized {early_ft_score}→{current_score}"
                              f" in knockout — deleting early FT post.")
                        _delete_early_post(match_id, 'FT')
                        with STATE_LOCK:
                            MATCH_STATE[match_id]['early_ft_score'] = list(current_score)
                            _save_state()
                    elif not _wait_for_scorers(match_id, lagging):
                        print(f"[{match_id}] FT score changed {early_ft_score}→{current_score} — correcting…")
                        _delete_early_post(match_id, 'FT')
                        cid, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                        if cid and ig_id:
                            with STATE_LOCK:
                                s = MATCH_STATE[match_id]
                                s['early_ft_ig_id']         = ig_id
                                s['early_ft_cloudinary_id'] = cid
                                s['early_ft_score']         = list(current_score)
                                _save_state()

        # ══════════════════════════════════════════════════════════════════════
        # FT TRIGGER  (scraper reports the match finished)
        # ══════════════════════════════════════════════════════════════════════
        elif new_status == 'ft' and not ft_posted and not is_event_posted(match_id, 'FT'):
            with STATE_LOCK:
                active_ft_ig = MATCH_STATE[match_id].get('early_ft_ig_id')
                _eft_done    = MATCH_STATE[match_id].get('early_ft_posted', False)

            if _eft_done and active_ft_ig:
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
                and not ft_posted
                and not is_event_posted(match_id, 'FT')):
            print(f"[{match_id}] [{raw_status}] minute={minute} — evaluating…")
            home_s, away_s = current_score
            is_draw        = (home_s == away_s)
            with STATE_LOCK:
                active_ft_ig   = MATCH_STATE[match_id].get('early_ft_ig_id')
                early_ft_score = (tuple(MATCH_STATE[match_id]['early_ft_score'])
                                  if MATCH_STATE[match_id].get('early_ft_score') else None)

            if raw_status == 'AP':
                # A shootout is under way. Any early ET card is now wrong — drop
                # it and wait; 'Played' arrives as FT and reposts with the
                # shootout result. Clearing the IG id (not the posted flag) is
                # what routes the FT trigger to a full repost.
                if active_ft_ig:
                    print(f"[{match_id}] Going to penalties — deleting ET early post…")
                    _delete_early_post(match_id, 'FT')
                print(f"[{match_id}] Penalty shootout in progress — waiting for the final result…")

            elif raw_status == 'ET':
                if not is_draw:
                    if not active_ft_ig:
                        if minute < 119:
                            print(f"[{match_id}] Goal in ET score={current_score} "
                                  f"minute={minute} — waiting until 119' to post early…")
                        elif not _wait_for_scorers(match_id, lagging):
                            print(f"[{match_id}] Minute={minute}, ET score={current_score} — posting early…")
                            cid, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                            if cid and ig_id:
                                with STATE_LOCK:
                                    s = MATCH_STATE[match_id]
                                    s['early_ft_posted']        = True
                                    s['early_ft_ig_id']         = ig_id
                                    s['early_ft_cloudinary_id'] = cid
                                    s['early_ft_score']         = list(current_score)
                                    _save_state()
                    elif (early_ft_score and current_score != early_ft_score
                            and not _wait_for_scorers(match_id, lagging)):
                        print(f"[{match_id}] ET score changed {early_ft_score}→{current_score} — correcting…")
                        _delete_early_post(match_id, 'FT')
                        cid, ig_id = _early_pipeline(entry, 'FT', scraper_data)
                        if cid and ig_id:
                            with STATE_LOCK:
                                s = MATCH_STATE[match_id]
                                s['early_ft_ig_id']         = ig_id
                                s['early_ft_cloudinary_id'] = cid
                                s['early_ft_score']         = list(current_score)
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


def _worker_safe(entry: dict):
    """Run match_worker and alert if it crashes — a dead thread is silent."""
    match_id = entry['match_id']
    try:
        match_worker(entry)
    except Exception as e:
        traceback.print_exc()
        with WORKERS_LOCK:
            ACTIVE_WORKERS.discard(match_id)
        send_alert(
            f"❌ {match_id}: the match worker CRASHED ({e}).\n"
            f"Live monitoring for {entry['home_team']} vs {entry['away_team']} "
            f"stopped — it will be respawned on the next registry check if the "
            f"match window is still open."
        )


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
                f"❌ matches.json: match {match_id} has a bad/missing "
                f"kickoff_utc ({e}).\nThis match will never get a worker "
                f"until the entry is fixed.",
                key=f'registry:kickoff:{match_id}', cooldown=1800,
            )
            continue

        window_open  = kickoff - timedelta(seconds=PRE_MATCH_WINDOW)
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