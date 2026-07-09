"""
Runs telegram_bot.py for as long as at least one match worker is active.

Started by telegram_bot.yml (dispatched from the dispatcher when a match
worker is running). Every CHECK_EVERY seconds it lists match_bot.yml runs;
when none are queued or in progress, it stops the bot and exits so the
Actions job ends — no manual on/off needed.

A startup grace period covers the race where the dispatcher fires the first
match worker and the bot in the same cycle: the worker may still be queued
(or not yet visible in the API) when this watchdog first checks.
"""

import os
import subprocess
import sys
import time

import requests

TOKEN = os.environ['GH_TOKEN']
REPO = os.environ['GITHUB_REPOSITORY']
API = 'https://api.github.com'
HEADS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}

CHECK_EVERY = 5 * 60       # seconds between checks
STARTUP_GRACE = 10 * 60    # don't check at all for the first 10 minutes


def match_workers_active() -> bool:
    for status in ('in_progress', 'queued'):
        resp = requests.get(
            f'{API}/repos/{REPO}/actions/workflows/match_bot.yml/runs',
            params={'status': status, 'per_page': 50},
            headers=HEADS,
            timeout=10,
        )
        resp.raise_for_status()
        for run in resp.json().get('workflow_runs', []):
            if run.get('name', '').startswith('match-'):
                return True
    return False


def main():
    bot = subprocess.Popen([sys.executable, 'telegram_bot.py'])
    print(f'[watchdog] Bot started (pid {bot.pid}). First check in {STARTUP_GRACE // 60} min.')
    time.sleep(STARTUP_GRACE)

    while True:
        if bot.poll() is not None:
            print(f'[watchdog] Bot exited on its own (code {bot.returncode}).')
            sys.exit(bot.returncode or 1)

        try:
            active = match_workers_active()
        except Exception as e:
            # Transient API failure — keep the bot alive rather than flapping.
            print(f'[watchdog] Worker check failed ({e}) — keeping bot alive.')
            active = True

        if not active:
            print('[watchdog] No active match workers — shutting down bot.')
            bot.terminate()
            try:
                bot.wait(timeout=30)
            except subprocess.TimeoutExpired:
                bot.kill()
            return

        time.sleep(CHECK_EVERY)


if __name__ == '__main__':
    main()
