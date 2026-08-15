# matchlog.py
"""
Per-match event log — the one place to find out what happened to a match.

Every match runs in its own GitHub Actions runner: separate filesystem,
separate SQLite, nothing shared, and the whole thing thrown away when the run
ends. `bot.db`, `state.json` and `data/` are gitignored, so after a match the
only surviving record was the run's stdout, buried in the Actions UI and
deleted after 90 days. This module fixes that.

The split mirrors the carousel's, for the same reason:

  worker      → appends events to Cloudinary  logs/<match_id>.json
                (sole writer for its own match, so no contention)
  dispatcher  → renders every log into  logs/<YYYY-MM-DD>.md  and commits it
                to master, where it is browsable and permanent

Timestamps are stored in UTC — unambiguous, and what the scraper speaks — and
rendered in German local time, which is what the person reading them wants.

Logging must never break a match. Every public function here swallows its own
exceptions: a log that fails to write is worth strictly less than a post.
"""

import atexit
import contextvars
import io
import json
import os
import signal
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

FOLDER = 'logs'
DISPLAY_TZ = ZoneInfo('Europe/Berlin')

# Most poll iterations produce no events at all, so this rarely delays
# anything; it exists to stop a burst of events becoming a burst of uploads.
FLUSH_DEBOUNCE_SECS = 60

INFO, WARN, ERROR = 'info', 'warn', 'error'
_MARK = {INFO: ' ', WARN: '⚠', ERROR: '✖'}

# A worker process handles exactly one match, but main.py's local mode runs
# several in threads. A ContextVar gives each thread its own value, so both
# work without the caller having to thread a match_id through every call.
_current = contextvars.ContextVar('matchlog_current', default=None)

_logs: dict[str, dict] = {}
_lock = threading.Lock()


# ── Paths ─────────────────────────────────────────────────────────────────────

def _log_id(match_id: str) -> str:
    return f'{FOLDER}/{match_id}.json'


def _log_url(match_id: str) -> str:
    cloud = os.getenv('CLOUDINARY_CLOUD_NAME')
    return f'https://res.cloudinary.com/{cloud}/raw/upload/{_log_id(match_id)}'


# ── Reading and writing ───────────────────────────────────────────────────────

def _read_remote(match_id: str) -> dict | None:
    """The log already stored for this match, or None."""
    try:
        # Cache-buster: a log written minutes ago must not be masked by a CDN
        # copy of the 404 that preceded it.
        response = requests.get(_log_url(match_id),
                                params={'_': int(datetime.now().timestamp())},
                                timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_remote(doc: dict) -> bool:
    try:
        blob = io.BytesIO(json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'))
        cloudinary.uploader.upload(
            blob,
            resource_type='raw',
            public_id=_log_id(str(doc['match_id'])),
            overwrite=True,
            invalidate=True,
        )
        return True
    except Exception as e:
        print(f'[matchlog] Could not write log for {doc.get("match_id")}: {e}')
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def start(entry: dict, run_note: str = '') -> None:
    """
    Begin logging for a match and bind it to this thread.

    An existing log is continued rather than replaced. A worker that dies is
    respawned by the dispatcher, and the record of *why it died* is the most
    valuable thing in the file — overwriting it on restart would delete the
    only evidence at the exact moment it started mattering.
    """
    try:
        match_id = str(entry['match_id'])
        _current.set(match_id)

        doc = _read_remote(match_id)
        fresh = doc is None
        if fresh:
            doc = {'match_id': match_id, 'events': []}

        doc.update({
            'home_team':   entry.get('home_team', '?'),
            'away_team':   entry.get('away_team', '?'),
            'competition': entry.get('competition', ''),
            'kickoff_utc': entry.get('kickoff_utc', ''),
            'group':       str(entry.get('carousel_group') or '') or None,
            'scraper_url': entry.get('scraper_url', ''),
        })
        doc.setdefault('runs', 0)
        doc['runs'] += 1

        with _lock:
            _logs[match_id] = {'doc': doc, 'dirty': True, 'last_flush': 0.0}

        note = run_note or ('first run' if fresh else f'run {doc["runs"]}')
        log('worker started', note, force=True)
    except Exception as e:
        print(f'[matchlog] start failed: {e}')


def bind(match_id: str) -> None:
    """Point this thread at a match without touching the stored log."""
    try:
        _current.set(str(match_id))
    except Exception:
        pass


def log(event: str, detail: str = '', level: str = INFO,
        match_id: str | None = None, force: bool = False) -> None:
    """Record one milestone. Also prints, so the Actions log keeps its copy."""
    try:
        mid = str(match_id or _current.get() or '')
        stamp = datetime.now(timezone.utc)
        local = stamp.astimezone(DISPLAY_TZ).strftime('%H:%M')
        print(f'[log {mid}] {local} {_MARK.get(level, " ")} {event}'
              f'{" — " + detail if detail else ""}')

        if not mid:
            return
        with _lock:
            state = _logs.get(mid)
            if state is None:
                # Logging before start() — keep it rather than lose it.
                state = {'doc': {'match_id': mid, 'events': []},
                         'dirty': True, 'last_flush': 0.0}
                _logs[mid] = state
            state['doc']['events'].append({
                'ts': stamp.isoformat(timespec='seconds'),
                'level': level,
                'event': event,
                'detail': detail,
            })
            state['dirty'] = True
        flush(mid, force=force or level in (WARN, ERROR))
    except Exception as e:
        print(f'[matchlog] log failed: {e}')


def warn(event: str, detail: str = '', **kw) -> None:
    log(event, detail, level=WARN, **kw)


def error(event: str, detail: str = '', **kw) -> None:
    log(event, detail, level=ERROR, **kw)


def flush(match_id: str | None = None, force: bool = False) -> None:
    """Push pending events to Cloudinary, debounced unless forced."""
    try:
        mid = str(match_id or _current.get() or '')
        with _lock:
            state = _logs.get(mid)
            if not state or not state['dirty']:
                return
            now = datetime.now(timezone.utc).timestamp()
            if not force and now - state['last_flush'] < FLUSH_DEBOUNCE_SECS:
                return
            doc = json.loads(json.dumps(state['doc']))   # snapshot, lock-free write
            state['last_flush'] = now
        if _write_remote(doc):
            with _lock:
                if mid in _logs:
                    _logs[mid]['dirty'] = False
    except Exception as e:
        print(f'[matchlog] flush failed: {e}')


def finish(outcome: str, detail: str = '', level: str = INFO,
           match_id: str | None = None) -> None:
    """Final entry for a run, always written through."""
    log(outcome, detail, level=level, match_id=match_id, force=True)


@atexit.register
def _flush_all() -> None:
    """Backstop: never let a process exit holding unwritten events."""
    try:
        for mid in list(_logs):
            flush(mid, force=True)
    except Exception:
        pass


def _on_sigterm(signum, frame):
    """
    Actions sends SIGTERM when a job hits its timeout or is cancelled, and
    Python does not run atexit handlers for a signal. That is precisely the
    run whose log you would want, so flush before letting the default
    behaviour take over.
    """
    _flush_all()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


try:
    # Only the main thread may install handlers; in main.py's threaded local
    # mode the workers simply inherit the one the main thread set.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_sigterm)
except Exception:
    pass
