"""
Single-match entry point for GitHub Actions.
Usage:  python3 match_worker_runner.py <match_id>

Looks up the entry in matches.json and runs the full match_worker loop:
  poll TheSportsDB → scrape at HT/FT → build scorecard → post to Instagram → exit.
"""

import json
import sys

from database import init_db
from main import match_worker

if len(sys.argv) < 2:
    print("Usage: python3 match_worker_runner.py <match_id>")
    sys.exit(1)

target_id = sys.argv[1]

with open('matches.json', encoding='utf-8') as f:
    registry = json.load(f)

entry = next((e for e in registry if e['match_id'] == target_id), None)
if entry is None:
    print(f"[runner] match_id {target_id!r} not found in matches.json")
    sys.exit(1)

print(f"[runner] Starting worker — {entry['home_team']} vs {entry['away_team']} (id={target_id})")

init_db()
match_worker(entry)
