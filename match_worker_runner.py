"""
Single-match entry point for GitHub Actions.
Usage:  python3 match_worker_runner.py <match_id>

Looks up the entry in matches.json and runs the full match_worker loop:
  poll the scraper → detect HT/FT → build scorecard → post to Instagram → exit.

Everything here runs through _worker_safe, so a crash sends a Telegram alert
before the process dies. In production this file *is* the worker — a failure
that only shows up as a red Actions run is a failure nobody sees.
"""

import json
import sys
import traceback

from database import init_db
from main import _worker_safe
from telegram_notify import send_alert

if len(sys.argv) < 2:
    print("Usage: python3 match_worker_runner.py <match_id>")
    sys.exit(1)

target_id = sys.argv[1]

# Startup is outside _worker_safe's reach but can fail just as silently: a
# half-written matches.json or an unreadable database leaves the match
# uncovered with nothing posted and nothing said.
try:
    with open('matches.json', encoding='utf-8') as f:
        registry = json.load(f)

    entry = next((e for e in registry if e['match_id'] == target_id), None)
    if entry is None:
        print(f"[runner] match_id {target_id!r} not found in matches.json")
        send_alert(
            f"❌ A match the bot was told to follow (id {target_id}) is no "
            f"longer in the fixture list.\n\n"
            f"Nothing will be posted for it. If it should still be covered, "
            f"add it back to the fixture list."
        )
        sys.exit(1)

    print(f"[runner] Starting worker — {entry['home_team']} vs "
          f"{entry['away_team']} (id={target_id})")

    init_db()
except SystemExit:
    raise
except Exception as e:
    traceback.print_exc()
    send_alert(
        f"❌ The bot could not start following the match with id {target_id}.\n\n"
        f"No scorecards will go out for it. It will try again within a few "
        f"minutes, as long as the match is still on.\n\n"
        f"Technical detail: {e}"
    )
    sys.exit(1)

# Alerts on crash; exit non-zero so the run is visibly red as well.
sys.exit(0 if _worker_safe(entry) else 1)
