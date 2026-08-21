"""
Runs telegram_bot.py for as long as it has anything to do.

Started by telegram_bot.yml (dispatched from the dispatcher when a match
worker is running, or when someone has messaged the bot while it was down).
Every CHECK_EVERY seconds it asks two questions; while either answers yes,
the bot stays up:

  1. Is a match worker queued or in progress?  — photos belong to live fixtures
  2. Has a human touched the bot recently?     — SESSION_FILE's mtime

The second exists for /card, which builds a match the scraper never saw. That
flow is used precisely when nothing is running, so worker activity alone would
shut the bot down in the middle of someone typing in a scoreline.

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

# Written by telegram_bot.py on every update it handles. Long enough that
# thinking about a scoreline, or finding the right photo, doesn't end the
# session; short enough that a forgotten conversation doesn't hold an Actions
# runner open for the full 350-minute job timeout.
SESSION_FILE = '.bot_session'
SESSION_IDLE = 20 * 60


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


def session_active() -> bool:
    """True if someone has messaged the bot within SESSION_IDLE.

    A missing file means no, which is the state at startup — the grace period,
    not this, is what keeps a freshly woken bot alive long enough to be used.
    """
    try:
        return time.time() - os.path.getmtime(SESSION_FILE) < SESSION_IDLE
    except OSError:
        return False


def main():
    # A session file left behind by an earlier run on a reused runner would
    # read as a conversation that is already over.
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass

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

        if not active and session_active():
            print('[watchdog] No match workers, but someone is mid-conversation '
                  '— keeping bot alive.')
            active = True

        if not active:
            print('[watchdog] No active match workers, nobody talking '
                  '— shutting down bot.')
            bot.terminate()
            try:
                bot.wait(timeout=30)
            except subprocess.TimeoutExpired:
                bot.kill()
            return

        time.sleep(CHECK_EVERY)


if __name__ == '__main__':
    main()
