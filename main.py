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
from cloudinary_upload import upload_image, upload_match_data
from database import init_db, is_event_posted, mark_event_posted, upsert_match
from football_scraper_dom import get_match_data
from instagram import post_to_instagram
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
# detection will stop it sooner in practice — 130 min covers ET + penalties)
MAX_MATCH_DURATION  = 130 * 60

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

def fetch_sportsdb_status(sportsdb_event_id: str) -> str | None:
    """
    Returns strStatus string from TheSportsDB, e.g. 'NS', '1H', 'HT',
    '2H', 'ET', 'PEN', 'FT', 'AET', 'ABD', 'PPD'.
    Returns None on network / parse errors.
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
            return events[0].get('strStatus')
    except Exception as e:
        print(f"[sportsdb] Error fetching event {sportsdb_event_id}: {e}")
    return None


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

def _run_pipeline(entry: dict, event_type: str, scraper_data: dict):
    """
    Shared pipeline for both HT and FT:
      scraper_data → scorecard image → Cloudinary → caption → Instagram → DB
    event_type: 'HT' or 'FT'
    """
    match_id = entry['match_id']
    print(f"[{match_id}] Running {event_type} pipeline…")

    try:
        image_path = generate_scorecard(scraper_data, event_type=event_type, match_id_override=match_id)
        public_url = upload_image(image_path)
        os.remove(image_path)
        caption = generate_caption(scraper_data, event_type=event_type)
        ig_id   = post_to_instagram(public_url, caption)
        mark_event_posted(match_id, event_type)
        print(f"[{match_id}] ✅ {event_type} posted — IG ID: {ig_id}")
    except Exception as e:
        print(f"[{match_id}] ❌ Pipeline error ({event_type}): {e}")
        # Do NOT mark as posted — will retry on next poll if status is unchanged


# ── Per-match worker ──────────────────────────────────────────────────────────

def match_worker(entry: dict):
    """
    Runs in its own thread. Polls TheSportsDB until FT, firing pipelines at
    HT and FT. Exits cleanly when FT is confirmed or the safety ceiling is hit.
    """
    match_id          = entry['match_id']
    sportsdb_event_id = entry['sportsdb_event_id']
    scraper_url       = entry['scraper_url']

    print(f"[{match_id}] Worker started — "
          f"{entry['home_team']} vs {entry['away_team']}")

    # Upsert into the DB registry so we have a record regardless of outcome
    upsert_match(entry)

    kickoff_utc  = datetime.fromisoformat(entry['kickoff_utc'].replace('Z', '+00:00'))
    deadline_utc = kickoff_utc + timedelta(seconds=MAX_MATCH_DURATION)

    while True:
        now = datetime.now(timezone.utc)

        if now > deadline_utc:
            print(f"[{match_id}] Safety ceiling reached — stopping worker.")
            break

        # ── Poll TheSportsDB ──────────────────────────────────────────────────
        raw_status = fetch_sportsdb_status(sportsdb_event_id)

        if raw_status is None:
            # Network blip — wait and retry
            time.sleep(POLL_INTERVAL_SECS)
            continue

        # Normalise to internal status tokens
        if raw_status in ('FT', 'AET'):
            new_status = 'ft'
        elif raw_status == 'HT':
            new_status = 'ht'
        elif raw_status in ('1H', '2H', 'ET', 'PEN'):
            new_status = 'live'
        else:
            # NS / PPD / ABD / unknown — not started or abandoned
            new_status = 'scheduled'

        with STATE_LOCK:
            current = MATCH_STATE.setdefault(match_id, {
                'status': 'scheduled', 'ht_posted': False, 'ft_posted': False
            })
            current['status'] = new_status
            _save_state()

        ht_posted = MATCH_STATE[match_id]['ht_posted']
        ft_posted = MATCH_STATE[match_id]['ft_posted']

        print(f"[{match_id}] status={raw_status}  ht_posted={ht_posted}  ft_posted={ft_posted}")

        # ── HT trigger ───────────────────────────────────────────────────────
        if new_status == 'ht' and not ht_posted and not is_event_posted(match_id, 'HT') \
                and entry.get('post_ht', True):
            print(f"[{match_id}] Half-time detected — fetching scraper data…")
            scraper_data = get_match_data(scraper_url)
            if scraper_data:
                _save_match_data(entry, scraper_data)
                _run_pipeline(entry, 'HT', scraper_data)
                with STATE_LOCK:
                    MATCH_STATE[match_id]['ht_posted'] = True
                    _save_state()

        # ── FT trigger ───────────────────────────────────────────────────────
        elif new_status == 'ft' and not ft_posted and not is_event_posted(match_id, 'FT'):
            print(f"[{match_id}] Full-time detected — fetching scraper data…")
            scraper_data = get_match_data(scraper_url)
            if scraper_data is None:
                # Scraper failed — fall back to cached file from earlier in the match
                cached_path = _data_path(entry)
                if os.path.exists(cached_path):
                    print(f"[{match_id}] Scraper failed — using cached data from {cached_path}")
                    with open(cached_path, encoding='utf-8') as _f:
                        scraper_data = json.load(_f)
            if scraper_data:
                _save_match_data(entry, scraper_data)
                _run_pipeline(entry, 'FT', scraper_data)
                with STATE_LOCK:
                    MATCH_STATE[match_id]['ft_posted'] = True
                    _save_state()
                _archive_match_data(entry)
                break  # Worker's job is done
            else:
                print(f"[{match_id}] No FT data available — will retry next poll.")

        if new_status == 'ft' and (MATCH_STATE[match_id].get('ft_posted') or is_event_posted(match_id, 'FT')):
            # FT already posted in a previous run — exit cleanly
            break

        time.sleep(POLL_INTERVAL_SECS)

    # Clean up so the same match can be re-spawned if needed (e.g. restart)
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