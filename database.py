# database.py — SQLite helpers
"""
Two tables:

  match_registry   — one row per match, written when its worker starts.
                     Useful for auditing and manual inspection.

  posted_events    — one row per (match_id, event_type) pair that has been
                     successfully posted to Instagram.
                     event_type is 'LINEUPS' (the pre-match starting XI
                     carousel), 'HT' or 'FT'.
                     Guards against a double post *within a run*: the worker
                     checks it before every card, so a retry inside the poll
                     loop can't repeat one.

                     It does NOT survive a restart in production. bot.db is
                     gitignored and every Actions run checks out fresh, so this
                     table starts empty each time. What actually stops a match
                     being posted twice is the dispatcher refusing to start a
                     second worker (active_match_ids in .github/scripts/
                     dispatcher.py). Making this guard real would mean storing
                     it off-box — Cloudinary already holds the match data.
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
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS posted_events (
                match_id    TEXT    NOT NULL,
                event_type  TEXT    NOT NULL,   -- 'LINEUPS', 'HT' or 'FT'
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
                (match_id, home_team, away_team, kickoff_utc, scraper_url)
            VALUES (:match_id, :home_team, :away_team, :kickoff_utc, :scraper_url)
            ON CONFLICT(match_id) DO UPDATE SET
                home_team   = excluded.home_team,
                away_team   = excluded.away_team,
                kickoff_utc = excluded.kickoff_utc,
                scraper_url = excluded.scraper_url
        ''', {k: entry[k] for k in
              ('match_id', 'home_team', 'away_team', 'kickoff_utc', 'scraper_url')})


def is_event_posted(match_id: str, event_type: str) -> bool:
    """Return True if this (match_id, event_type) pair has been posted."""
    with _conn() as conn:
        row = conn.execute(
            'SELECT 1 FROM posted_events WHERE match_id=? AND event_type=?',
            (match_id, event_type)
        ).fetchone()
    return row is not None


def mark_event_posted(match_id: str, event_type: str):
    """Record that event_type ('LINEUPS', 'HT' or 'FT') has been posted."""
    with _conn() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO posted_events (match_id, event_type) VALUES (?, ?)',
            (match_id, event_type)
        )