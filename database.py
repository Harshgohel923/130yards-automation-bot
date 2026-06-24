# database.py — SQLite helpers
"""
Two tables:

  match_registry   — one row per match, written when its worker starts.
                     Useful for auditing and manual inspection.

  posted_events    — one row per (match_id, event_type) pair that has been
                     successfully posted to Instagram.
                     event_type is either 'HT' or 'FT'.
                     This is the idempotency guard: even if the process
                     restarts mid-match, we never post the same scorecard twice.
"""

import sqlite3
from contextlib import contextmanager

DB_PATH = 'bot.db'


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS match_registry (
                match_id        TEXT PRIMARY KEY,
                home_team       TEXT,
                away_team       TEXT,
                kickoff_utc     TEXT,
                scraper_url     TEXT,
                sportsdb_event_id TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS posted_events (
                match_id    TEXT    NOT NULL,
                event_type  TEXT    NOT NULL,   -- 'HT' or 'FT'
                posted_at   TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (match_id, event_type)
            );
        ''')
    print("[db] Initialised bot.db")


def upsert_match(entry: dict):
    """Insert or update a match in the registry."""
    with _conn() as conn:
        conn.execute('''
            INSERT INTO match_registry
                (match_id, home_team, away_team, kickoff_utc, scraper_url, sportsdb_event_id)
            VALUES (:match_id, :home_team, :away_team, :kickoff_utc, :scraper_url, :sportsdb_event_id)
            ON CONFLICT(match_id) DO UPDATE SET
                home_team         = excluded.home_team,
                away_team         = excluded.away_team,
                kickoff_utc       = excluded.kickoff_utc,
                scraper_url       = excluded.scraper_url,
                sportsdb_event_id = excluded.sportsdb_event_id
        ''', entry)


def is_event_posted(match_id: str, event_type: str) -> bool:
    """Return True if this (match_id, event_type) pair has been posted."""
    with _conn() as conn:
        row = conn.execute(
            'SELECT 1 FROM posted_events WHERE match_id=? AND event_type=?',
            (match_id, event_type)
        ).fetchone()
    return row is not None


def mark_event_posted(match_id: str, event_type: str):
    """Record that event_type ('HT' or 'FT') has been posted for match_id."""
    with _conn() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO posted_events (match_id, event_type) VALUES (?, ?)',
            (match_id, event_type)
        )