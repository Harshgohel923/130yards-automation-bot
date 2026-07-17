# main.py
"""
Bot orchestrator.

Match registry lives in matches.json — edit it freely while the bot is running.
The scheduler re-reads it every REGISTRY_POLL_SECS seconds so new entries are
picked up automatically without a restart.

Per-match flow
──────────────
  scheduled  →  (kickoff window opens)
  →  live      →  poll TheSportsDB every POLL_INTERVAL_SECS
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from caption import generate_caption
from cloudinary_upload import upload_image, upload_match_data, delete_image
from database import init_db, is_event_posted, mark_event_posted, upsert_match
from football_scraper_dom import get_match_data
from cloudinary_utils import fetch_match_photo
from instagram import post_to_instagram, delete_instagram_post
from overlay_scorebar import generate_overlay_scorecard
from scorecard import generate_scorecard

load_dotenv()

# ── Tunables ──────────────────────────────────────────────────────────────────
REGISTRY_FILE       = 'matches.json'
STATE_FILE          = 'state.json'
POLL_INTERVAL_SECS  = 60       # how often to hit TheSportsDB during a live match
REGISTRY_POLL_SECS  = 300      # how often to re-read matches.json for new entries
# Start monitoring this many seconds before kickoff
PRE_MATCH_WINDOW    = 5 * 60
# Stop polling this many seconds after scheduled kickoff (safety ceiling; FT
# detection will stop it sooner in practice — 210 min covers ET + full penalty shootout)
MAX_MATCH_DURATION  = 210 * 60

SPORTSDB_BASE = 'https://www.thesportsdb.com/api/v1/json/123/lookupevent.php'

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
        return []


# ── TheSportsDB helper ────────────────────────────────────────────────────────

def fetch_sportsdb_status(sportsdb_event_id: str) -> tuple[str | None, str | None, str | None]:
    """
    Returns (strStatus, intRound, strLeague) from TheSportsDB.
    All three are None on network / parse errors.
    """
    try:
        r = requests.get(
            SPORTSDB_BASE,
            params={'id': sportsdb_event_id},
            timeout=10
        )
        r.raise_for_status()
        events = r.json().get('events')
        if events:
            ev = events[0]
            return ev.get('strStatus'), ev.get('intRound'), ev.get('strLeague')
    except Exception as e:
        print(f"[sportsdb] Error fetching event {sportsdb_event_id}: {e}")
    return None, None, None


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


# ── Scorecard pipeline ────────────────────────────────────────────────────────

def _generate_card(entry: dict, event_type: str, scraper_data: dict,
                   int_round, str_league) -> str:
    """
    Build the scorecard image, preferring the photo-overlay style when a match
    photo has been uploaded via the Telegram bot; falls back to the classic
    template scorecard when there is no photo (or the overlay render fails).
    Returns the local image path.
    """
    match_id = entry['match_id']
    photo_path = fetch_match_photo(match_id, event_type)
    if photo_path:
        try:
            path = generate_overlay_scorecard(scraper_data, photo_path,
                                              event_type=event_type,
                                              match_id_override=match_id)
            print(f"[{match_id}] Using photo-overlay scorecard ({event_type}).")
            return path
        except Exception as e:
            print(f"[{match_id}] Overlay scorecard failed ({e}) — falling back to template.")
    return generate_scorecard(scraper_data, event_type=event_type,
                              match_id_override=match_id,
                              int_round=int_round, str_league=str_league)


def _run_pipeline(entry: dict, event_type: str, scraper_data: dict):
    """
    Shared pipeline for both HT and FT:
      scraper_data → scorecard image → Cloudinary → caption → Instagram → DB
    event_type: 'HT' or 'FT'
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running {event_type} pipeline…")

    with STATE_LOCK:
        _s = MATCH_STATE.get(match_id, {})
        _int_round  = _s.get('sportsdb_round')
        _str_league = _s.get('sportsdb_league')

    try:
        image_path = _generate_card(entry, event_type, scraper_data,
                                    _int_round, _str_league)
        public_url, _ = upload_image(image_path)
        os.remove(image_path)
        caption = generate_caption(scraper_data, event_type=event_type,
                                   records=entry.get('records'))
        ig_id   = post_to_instagram(public_url, caption)
        mark_event_posted(match_id, event_type)
        print(f"[{match_id}] ✅ {event_type} posted — IG ID: {ig_id}")
    except Exception as e:
        print(f"[{match_id}] ❌ Pipeline error ({event_type}): {e}")
        # Do NOT mark as posted — will retry on next poll if status is unchanged


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
    with STATE_LOCK:
        _s = MATCH_STATE.get(match_id, {})
        _int_round  = _s.get('sportsdb_round')
        _str_league = _s.get('sportsdb_league')
    try:
        image_path = _generate_card(entry, event_type, scraper_data,
                                    _int_round, _str_league)
        public_url, cid = upload_image(image_path)
        os.remove(image_path)
        caption = generate_caption(scraper_data, event_type=event_type,
                                   records=entry.get('records'))
        ig_id = post_to_instagram(public_url, caption)
        print(f"[{match_id}] ✅ Early {event_type} posted — IG ID: {ig_id}")
        return cid, ig_id
    except Exception as e:
        print(f"[{match_id}] ❌ Early {event_type} pipeline error: {e}")
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

    if ig_id:
        try:
            delete_instagram_post(ig_id)
        except Exception as e:
            print(f"[{match_id}] Warning: Instagram delete failed ({ig_id}): {e}")

    with STATE_LOCK:
        s = MATCH_STATE.get(match_id, {})
        s[key_cid] = None
        s[key_ig]  = None
        _save_state()


# ── Per-match worker ──────────────────────────────────────────────────────────

def match_worker(entry: dict):
    """
    Runs in its own thread. Polls TheSportsDB until FT, firing pipelines at
    HT and FT. Posts early scorecards at scraper minute 45/90 and corrects
    them if the score changes before the official whistle.
    """
    match_id          = entry['match_id']
    sportsdb_event_id = entry['sportsdb_event_id']
    scraper_url       = entry['scraper_url']
    is_knockout       = entry.get('knockout_match', False)

    print(f"[{match_id}] Worker started — {entry['home_team']} vs {entry['away_team']}"
          f"  (knockout={is_knockout})")

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
            '1h_started_at':          None,
            '2h_started_at':          None,
            'et_started_at':          None,
        }.items():
            s.setdefault(k, v)
        _save_state()

    while True:
        now = datetime.now(timezone.utc)

        if now > deadline_utc:
            print(f"[{match_id}] Safety ceiling reached — stopping worker.")
            break

        # ── Poll TheSportsDB ──────────────────────────────────────────────────
        raw_status, sdb_round, sdb_league = fetch_sportsdb_status(sportsdb_event_id)
        if raw_status is None:
            time.sleep(POLL_INTERVAL_SECS)
            continue

        # ── Record phase start times + round meta (once; persisted) ──────────
        with STATE_LOCK:
            s = MATCH_STATE[match_id]
            changed = False
            if raw_status == '1H' and not s.get('1h_started_at'):
                s['1h_started_at'] = now.isoformat()
                changed = True
                print(f"[{match_id}] 1H started — early HT monitoring begins in 43 min.")
            if raw_status == '2H' and not s.get('2h_started_at'):
                s['2h_started_at'] = now.isoformat()
                changed = True
                print(f"[{match_id}] 2H started — early FT monitoring begins in 43 min.")
            if raw_status == 'ET' and not s.get('et_started_at'):
                s['et_started_at'] = now.isoformat()
                changed = True
                print(f"[{match_id}] ET started — scraper checks begin in 29 min.")
            if sdb_round and not s.get('sportsdb_round'):
                s['sportsdb_round'] = str(sdb_round)
                changed = True
            if sdb_league and not s.get('sportsdb_league'):
                s['sportsdb_league'] = str(sdb_league)
                changed = True
            if changed:
                _save_state()

        # ── Normalise status ──────────────────────────────────────────────────
        if raw_status in ('FT', 'AET'):
            new_status = 'ft'
        elif raw_status == 'HT':
            new_status = 'ht'
        elif raw_status in ('1H', '2H', 'BT', 'ET', 'PEN', 'AP'):
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
            h1_ts           = s.get('1h_started_at')
            h2_ts           = s.get('2h_started_at')
            et_ts           = s.get('et_started_at')

        print(f"[{match_id}] status={raw_status}  ht_posted={ht_posted}  ft_posted={ft_posted}"
              f"  early_ht={early_ht_posted}  early_ft={early_ft_posted}")

        # ══════════════════════════════════════════════════════════════════════
        # EARLY HT MONITORING  (raw_status == '1H', 43 min elapsed)
        # ══════════════════════════════════════════════════════════════════════
        if (raw_status == '1H'
                and entry.get('post_ht', True)
                and not is_event_posted(match_id, 'HT')
                and h1_ts
                and (now - datetime.fromisoformat(h1_ts)).total_seconds() >= 43 * 60):

            scraper_data = get_match_data(scraper_url)
            if scraper_data:
                minute        = _get_minute(scraper_data)
                current_score = _get_score(scraper_data)
                lagging       = not _events_match_score(scraper_data)

                if (not early_ht_posted and minute >= 45
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
        # HT TRIGGER  (TheSportsDB confirms HT)
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
                scraper_data = get_match_data(scraper_url)
                if scraper_data:
                    if _wait_for_scorers(match_id, not _events_match_score(scraper_data)):
                        print(f"[{match_id}] HT confirmed but scorer list lagging — retrying next poll.")
                    else:
                        _save_match_data(entry, scraper_data)
                        _run_pipeline(entry, 'HT', scraper_data)
                        with STATE_LOCK:
                            MATCH_STATE[match_id]['ht_posted'] = True
                            _save_state()

        # ══════════════════════════════════════════════════════════════════════
        # EARLY FT MONITORING  (raw_status == '2H', 43 min elapsed)
        # ══════════════════════════════════════════════════════════════════════
        elif (raw_status == '2H'
                and not ft_posted
                and not is_event_posted(match_id, 'FT')
                and h2_ts
                and (now - datetime.fromisoformat(h2_ts)).total_seconds() >= 43 * 60):

            scraper_data = get_match_data(scraper_url)
            if scraper_data:
                minute        = _get_minute(scraper_data)
                current_score = _get_score(scraper_data)
                home_s, away_s = current_score
                is_draw = (home_s == away_s)
                lagging = not _events_match_score(scraper_data)

                if not early_ft_posted and minute >= 90:
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

                elif early_ft_posted and early_ft_ig_id and early_ft_score:
                    if current_score != early_ft_score:
                        new_is_draw = (home_s == away_s)
                        if is_knockout and new_is_draw:
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
        # FT TRIGGER  (TheSportsDB confirms FT / AET)
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
                scraper_data = get_match_data(scraper_url)
                if scraper_data is None:
                    cached_path = _data_path(entry)
                    if os.path.exists(cached_path):
                        print(f"[{match_id}] Using cached data from {cached_path}")
                        with open(cached_path, encoding='utf-8') as _f:
                            scraper_data = json.load(_f)
                if scraper_data:
                    if _wait_for_scorers(match_id, not _events_match_score(scraper_data)):
                        print(f"[{match_id}] FT confirmed but scorer list lagging — retrying next poll.")
                    else:
                        _save_match_data(entry, scraper_data)
                        _run_pipeline(entry, 'FT', scraper_data)
                        with STATE_LOCK:
                            MATCH_STATE[match_id]['ft_posted'] = True
                            _save_state()
                        _archive_match_data(entry)
                        break
                else:
                    print(f"[{match_id}] No FT data available — will retry next poll.")

        # ══════════════════════════════════════════════════════════════════════
        # ET / AP SCRAPER CHECK
        # ══════════════════════════════════════════════════════════════════════
        elif not ft_posted and not is_event_posted(match_id, 'FT'):
            et_elapsed   = (now - datetime.fromisoformat(et_ts)).total_seconds() if et_ts else 0
            should_check = (
                raw_status == 'AP' or
                (raw_status == 'ET' and et_elapsed >= 29 * 60)
            )
            if should_check:
                print(f"[{match_id}] [{raw_status}] Pinging scraper…")
                scraper_data = get_match_data(scraper_url)
                if scraper_data:
                    scraper_status = scraper_data.get('status', '')
                    current_score  = _get_score(scraper_data)
                    home_s, away_s = current_score
                    is_draw        = (home_s == away_s)
                    minute         = _get_minute(scraper_data)
                    lagging        = not _events_match_score(scraper_data)
                    ms             = scraper_data.get('matchSample', {})
                    has_penalties  = bool(
                        str(ms.get('ps_A') or '').strip() and
                        str(ms.get('ps_B') or '').strip()
                    )
                    with STATE_LOCK:
                        active_ft_ig   = MATCH_STATE[match_id].get('early_ft_ig_id')
                        early_ft_score = (tuple(MATCH_STATE[match_id]['early_ft_score'])
                                          if MATCH_STATE[match_id].get('early_ft_score') else None)

                    if raw_status == 'AP' and scraper_status == 'Played':
                        if _wait_for_scorers(match_id, lagging):
                            print(f"[{match_id}] Penalties finished but scorer list lagging — retrying next poll.")
                        else:
                            if active_ft_ig:
                                _delete_early_post(match_id, 'FT')
                            print(f"[{match_id}] Penalties finished — posting final scorecard…")
                            _save_match_data(entry, scraper_data)
                            _run_pipeline(entry, 'FT', scraper_data)
                            with STATE_LOCK:
                                MATCH_STATE[match_id]['ft_posted'] = True
                                _save_state()
                            _archive_match_data(entry)
                            break

                    elif raw_status == 'ET':
                        if has_penalties:
                            if active_ft_ig:
                                print(f"[{match_id}] Going to penalties — deleting ET early post…")
                                _delete_early_post(match_id, 'FT')
                                active_ft_ig = None
                            print(f"[{match_id}] Penalty shootout in progress — waiting for AP/Played…")

                        elif scraper_status == 'Played':
                            score_unchanged = (active_ft_ig and early_ft_score
                                               and current_score == early_ft_score)
                            if (not score_unchanged
                                    and _wait_for_scorers(match_id, lagging)):
                                print(f"[{match_id}] ET finished but scorer list lagging — retrying next poll.")
                            else:
                                if score_unchanged:
                                    print(f"[{match_id}] ET finished, score unchanged — marking done.")
                                    mark_event_posted(match_id, 'FT')
                                else:
                                    if active_ft_ig:
                                        _delete_early_post(match_id, 'FT')
                                    print(f"[{match_id}] ET finished — posting final scorecard…")
                                    _save_match_data(entry, scraper_data)
                                    _run_pipeline(entry, 'FT', scraper_data)
                                with STATE_LOCK:
                                    MATCH_STATE[match_id]['ft_posted'] = True
                                    _save_state()
                                _archive_match_data(entry)
                                break

                        elif not is_draw:
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
                            print(f"[{match_id}] ET scraper status={scraper_status!r} — still in progress.")
                else:
                    print(f"[{match_id}] Scraper fetch failed during {raw_status}.")

        # Clean exit if FT was already posted in a previous run
        if new_status == 'ft' and (MATCH_STATE[match_id].get('ft_posted')
                                   or is_event_posted(match_id, 'FT')):
            break

        time.sleep(POLL_INTERVAL_SECS)

    with WORKERS_LOCK:
        ACTIVE_WORKERS.discard(match_id)
    print(f"[{match_id}] Worker exited.")


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
            continue

        window_open  = kickoff - timedelta(seconds=PRE_MATCH_WINDOW)
        window_close = kickoff + timedelta(seconds=MAX_MATCH_DURATION)

        if window_open <= now <= window_close:
            print(f"[registry] Spawning worker for match {match_id} "
                  f"({entry['home_team']} vs {entry['away_team']})")
            with WORKERS_LOCK:
                ACTIVE_WORKERS.add(match_id)
            executor.submit(match_worker, entry)
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