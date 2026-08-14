"""
Reads matches.json, finds matches whose pre-match window opens within
the next 15 minutes, and triggers a match_bot.yml workflow run for each —
unless a worker for that match is already running.

Dedup strategy: match_bot.yml sets run-name to "match-{match_id}".
We list all in-progress runs of that workflow and extract match IDs from
their names — zero external state required.

It also publishes completed carousel groups (see carousel.py) and prunes
finished fixtures out of matches.json, committing the trimmed file back to
master so the registry doesn't grow without bound.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import requests

# This script lives in .github/scripts, but the bot's own modules (carousel,
# caption, instagram…) sit at the repo root. Running a script by path puts only
# its own directory on sys.path, so the root has to be added explicitly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

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

# How long after kickoff a fixture stops being of any possible use. A worker
# gives up at kickoff + 210 min (MAX_MATCH_DURATION), so by six hours there is
# nothing left that could still act on the entry.
PRUNE_AFTER_HOURS = 6
MATCHES_FILE = os.path.join(REPO_ROOT, 'matches.json')
GIT_AUTHOR_NAME  = '130 Yards Bot'
GIT_AUTHOR_EMAIL = 'bot@users.noreply.github.com'


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


def telegram_bot_running() -> bool:
    """True if a telegram-bot run is queued or in progress."""
    for status in ('in_progress', 'queued'):
        resp = requests.get(
            f'{API}/repos/{REPO}/actions/workflows/telegram_bot.yml/runs',
            params={'status': status, 'per_page': 10},
            headers=HEADS,
            timeout=10,
        )
        resp.raise_for_status()
        if resp.json().get('workflow_runs'):
            return True
    return False


def ensure_telegram_bot():
    """Start the photo-intake Telegram bot if it isn't already running.
    Best-effort: a bot failure must never block match worker dispatching.
    The bot's own watchdog shuts it down once no match workers remain."""
    try:
        if telegram_bot_running():
            print('[dispatcher] Telegram bot already running')
            return
        resp = requests.post(
            f'{API}/repos/{REPO}/actions/workflows/telegram_bot.yml/dispatches',
            headers=HEADS,
            json={'ref': 'master'},
            timeout=10,
        )
        if resp.status_code == 204:
            print('[dispatcher] Triggered Telegram bot')
        else:
            print(f'[dispatcher] WARNING: could not trigger Telegram bot: '
                  f'{resp.status_code} {resp.text}')
    except Exception as e:
        print(f'[dispatcher] WARNING: Telegram bot check failed: {e}')


def publish_carousel_groups(registry: list):
    """
    Post any carousel group whose matches have all finished.

    The dispatcher owns this rather than the last worker to finish: it is a
    single serialised process that keeps running every 15 minutes, so a group
    can't be stranded by a worker that died after its match, and two matches
    ending in the same minute can't race into a double post.

    Imported lazily so a problem in the posting stack — a bad token, a
    Cloudinary outage — can never stop match workers from being dispatched.
    """
    try:
        from carousel import groups_in, publish_group
    except Exception as e:
        print(f'[dispatcher] WARNING: carousel support unavailable ({e})')
        return

    for group, entries in groups_in(registry).items():
        try:
            publish_group(group, entries)
        except Exception as e:
            print(f'[dispatcher] ERROR publishing carousel {group}: {e}')
            try:
                from telegram_notify import send_alert
                send_alert(
                    f"❌ The '{group}' carousel couldn't be posted.\n\n"
                    f"Every scorecard in it is safely saved, so nothing is "
                    f"lost. The bot tries again by itself every 15 minutes.\n\n"
                    f"Technical detail: {e}",
                    key=f'carousel:{group}:publish', cooldown=1800,
                )
            except Exception:
                pass


def _pending_carousel_groups(registry: list) -> set:
    """
    Groups that have not been posted yet.

    A grouped match must stay in the registry until its carousel goes out:
    publish_group() reads the group's membership straight from matches.json, so
    pruning a member early would quietly shrink the post — or, if every member
    went, drop the group on the floor entirely.
    """
    try:
        from carousel import groups_in, posted_marker
    except Exception as e:
        print(f'[dispatcher] WARNING: cannot check carousel groups ({e}) — '
              f'keeping every grouped fixture')
        return {str(e_.get('carousel_group')) for e_ in registry
                if isinstance(e_, dict) and e_.get('carousel_group')}

    pending = set()
    for group in groups_in(registry):
        try:
            if not posted_marker(group):
                pending.add(group)
        except Exception as e:
            print(f'[dispatcher] WARNING: could not check carousel {group} ({e}) — '
                  f'keeping its fixtures')
            pending.add(group)
    return pending


def _prune_reason(entry: dict, now, running: set, pending_groups: set) -> str | None:
    """Why this fixture can be dropped, or None to keep it."""
    match_id = str(entry.get('match_id', ''))
    if match_id in running:
        return None                       # still being covered right now

    try:
        kickoff = datetime.fromisoformat(str(entry['kickoff_utc']).replace('Z', '+00:00'))
    except (KeyError, ValueError):
        return None                       # unreadable date — leave it for a human

    if now < kickoff + timedelta(hours=PRUNE_AFTER_HOURS):
        return None                       # too soon to be sure it is done with

    group = str(entry.get('carousel_group') or '').strip()
    if group and group in pending_groups:
        return None                       # its carousel hasn't posted yet

    hours = (now - kickoff).total_seconds() / 3600
    return f'kicked off {hours:.0f}h ago'


def prune_finished_matches(registry: list, running: set):
    """
    Drop fixtures that can no longer need anything, and push the trimmed file.

    Conservative on purpose: a fixture only goes once it is past every deadline
    the bot has, has no worker running, and isn't holding up a carousel. A
    fixture that failed to post is dropped too — six hours on, no process will
    ever pick it up again, and the failure was alerted at the time.
    """
    now = datetime.now(timezone.utc)
    pending_groups = _pending_carousel_groups(registry)

    kept, removed = [], []
    for entry in registry:
        reason = _prune_reason(entry, now, running, pending_groups) if isinstance(entry, dict) else None
        if reason:
            removed.append((entry, reason))
        else:
            kept.append(entry)

    if not removed:
        print('[dispatcher] Nothing to prune from matches.json')
        return

    for entry, reason in removed:
        print(f'[dispatcher] Pruning {entry.get("match_id")} '
              f'({entry.get("home_team")} vs {entry.get("away_team")}) — {reason}')

    with open(MATCHES_FILE, 'w', encoding='utf-8') as f:
        json.dump(kept, f, indent=2, ensure_ascii=False)
        f.write('\n')

    lines = '\n'.join(f'- {e.get("home_team")} vs {e.get("away_team")} '
                      f'({e.get("kickoff_utc")})' for e, _ in removed)
    message = (f'chore: remove {len(removed)} finished '
               f'fixture{"s" if len(removed) != 1 else ""}\n\n{lines}\n')
    _push_registry(message)


def _git(*args) -> bool:
    """Run a git command in the repo. Logs and returns False on failure."""
    result = subprocess.run(('git', '-C', REPO_ROOT) + args,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f'[dispatcher] git {" ".join(args)} failed: '
              f'{(result.stderr or result.stdout).strip()[:300]}')
        return False
    return True


def _push_registry(message: str) -> None:
    """
    Commit matches.json and push it to master.

    Best-effort: this runner is thrown away either way, so a failed push costs
    nothing but a retry on the next tick. Rebasing first handles the case where
    a fixture was added by hand since this run checked out.
    """
    if not _git('config', 'user.name', GIT_AUTHOR_NAME):
        return
    if not _git('config', 'user.email', GIT_AUTHOR_EMAIL):
        return
    if not _git('add', 'matches.json'):
        return
    if not _git('commit', '-m', message):
        print('[dispatcher] Nothing staged — matches.json was already current')
        return
    # Pull anything pushed since checkout so the push can't be rejected for
    # being behind; if that fails, leave it — the next tick tries again.
    _git('pull', '--rebase', 'origin', 'master')
    if _git('push', 'origin', 'HEAD:master'):
        print('[dispatcher] Pushed the trimmed matches.json to master')


def main():
    with open(MATCHES_FILE, encoding='utf-8') as f:
        registry = json.load(f)

    now     = datetime.now(timezone.utc)
    running = active_match_ids()
    print(f'[dispatcher] {now.isoformat(timespec="seconds")}  active workers: {running or "none"}')

    fired_any = False
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
            fired_any = True
        elif now < window_open:
            mins = int((window_open - now).total_seconds() / 60)
            print(f'[dispatcher] {match_id} ({home} vs {away}) — opens in ~{mins} min')
        else:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — window passed, skip')

    # Keep the photo-intake Telegram bot up whenever any worker is active.
    # Its watchdog shuts it down once the last match worker finishes.
    if running or fired_any:
        ensure_telegram_bot()

    # Publish any carousel group that is now complete. Runs after dispatching so
    # a slow or failing post can never delay a worker for a kicking-off match.
    publish_carousel_groups(registry)

    # Then tidy the registry. After publishing, so a group that just went out
    # stops holding its fixtures back.
    prune_finished_matches(registry, running)


if __name__ == '__main__':
    main()
