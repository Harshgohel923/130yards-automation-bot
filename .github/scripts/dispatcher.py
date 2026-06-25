"""
Reads matches.json, finds matches whose pre-match window opens within
the next 15 minutes, and triggers a match_bot.yml workflow run for each —
unless a worker for that match is already running.

Dedup strategy: match_bot.yml sets run-name to "match-{match_id}".
We list all in-progress runs of that workflow and extract match IDs from
their names — zero external state required.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

TOKEN  = os.environ['GH_TOKEN']
REPO   = os.environ['GITHUB_REPOSITORY']
API    = 'https://api.github.com'
HEADS  = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}

PRE_MATCH_WINDOW_SECS = 30 * 60   # start worker 30 min before kickoff
DISPATCH_LOOKAHEAD    = 15 * 60   # how far ahead this dispatcher looks


def active_match_ids() -> set:
    """Return match IDs that already have an in-progress match_bot run."""
    resp = requests.get(
        f'{API}/repos/{REPO}/actions/workflows/match_bot.yml/runs',
        params={'status': 'in_progress', 'per_page': 50},
        headers=HEADS,
        timeout=10,
    )
    resp.raise_for_status()
    ids = set()
    for run in resp.json().get('workflow_runs', []):
        name = run.get('name', '')
        if name.startswith('match-'):
            ids.add(name[len('match-'):])
    return ids


def trigger_worker(match_id: str):
    resp = requests.post(
        f'{API}/repos/{REPO}/actions/workflows/match_bot.yml/dispatches',
        headers=HEADS,
        json={'ref': 'master', 'inputs': {'match_id': match_id}},
        timeout=10,
    )
    if resp.status_code == 204:
        print(f'[dispatcher] Triggered worker for match {match_id}')
    else:
        print(f'[dispatcher] ERROR triggering {match_id}: {resp.status_code} {resp.text}')
        sys.exit(1)


def main():
    with open('matches.json', encoding='utf-8') as f:
        registry = json.load(f)

    now     = datetime.now(timezone.utc)
    running = active_match_ids()
    print(f'[dispatcher] {now.isoformat(timespec="seconds")}  active workers: {running or "none"}')

    for entry in registry:
        match_id = entry['match_id']
        home     = entry.get('home_team', '?')
        away     = entry.get('away_team', '?')

        if match_id in running:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — already running, skip')
            continue

        try:
            kickoff = datetime.fromisoformat(entry['kickoff_utc'].replace('Z', '+00:00'))
        except (KeyError, ValueError) as e:
            print(f'[dispatcher] Bad kickoff_utc for {match_id}: {e}')
            continue

        window_open = kickoff - timedelta(seconds=PRE_MATCH_WINDOW_SECS)
        fire_by     = window_open + timedelta(seconds=DISPATCH_LOOKAHEAD)

        if window_open <= now <= fire_by:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — window open, firing worker')
            trigger_worker(match_id)
        elif now < window_open:
            mins = int((window_open - now).total_seconds() / 60)
            print(f'[dispatcher] {match_id} ({home} vs {away}) — opens in ~{mins} min')
        else:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — window passed, skip')


if __name__ == '__main__':
    main()
