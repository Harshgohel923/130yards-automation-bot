"""
Reads matches.json, finds matches whose pre-match window opens within
the next 15 minutes, and triggers a match_bot.yml workflow run for each —
unless a worker for that match is already running.

Dedup strategy: match_bot.yml sets run-name to "match-{match_id} · Home vs Away".
We list every queued or in-progress run of that workflow and extract match IDs
from their names — zero external state required. The team names are there so the
Actions list is readable; only the first token carries meaning.

It also publishes completed carousel groups (see carousel.py) and prunes
finished fixtures out of matches.json, committing the trimmed file back to
master so the registry doesn't grow without bound.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

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
# Fixtures with a team-news post start earlier: lineups are published around
# an hour out, and the worker must already be polling to catch them. Only
# those fixtures pay for the longer Actions run.
LINEUP_PRE_MATCH_WINDOW_SECS = 75 * 60
DISPATCH_LOOKAHEAD    = 15 * 60   # how far ahead this dispatcher looks

# When to queue a relief bot behind the one holding the token. The gap between
# this and the watchdog's own deadline (bot_watchdog.MAX_RUNTIME, 340 min) is
# the window a dispatcher tick has to land in; miss it and no successor is
# queued at all, which is silently the old behaviour. So the window is two
# ticks wide, not one — a schedule that runs a few minutes late must not be
# able to step over it. Queueing early is close to free: a pending run holds
# no runner and starts only when the incumbent lets the slot go.
BOT_HANDOFF_AFTER_SECS = 310 * 60

# How long after kickoff a fixture stops being of any possible use. A worker
# gives up at kickoff + 290 min (MAX_MATCH_DURATION), so by six hours there is
# nothing left that could still act on the entry.
PRUNE_AFTER_HOURS = 6

# Pruning per fixture meant one commit per distinct kickoff time — six on a
# normal Saturday, since a fixture crosses PRUNE_AFTER_HOURS whenever its own
# kickoff did, six hours earlier. It is pure housekeeping, so it is batched
# into a single daily pass instead.
#
# 10:00 German time: late enough that even an overnight MLS kickoff (02:30
# local) is past its six hours by 08:30, early enough to be well clear of the
# European afternoon. The dispatcher ticks four times inside the hour; the
# first one does the work and the rest find nothing left to prune, which also
# means a failed push simply retries fifteen minutes later.
PRUNE_TZ = ZoneInfo('Europe/Berlin')
PRUNE_HOUR_LOCAL = 10

# If the daily window is missed entirely — the external cron was down for an
# hour, say — don't let the registry grow until this time tomorrow.
PRUNE_CATCHUP_HOURS = 24

MATCHES_FILE = os.path.join(REPO_ROOT, 'matches.json')
GIT_AUTHOR_NAME  = '130 Yards Bot'
GIT_AUTHOR_EMAIL = 'bot@users.noreply.github.com'

# The run name can't say what a tick did — GitHub renders it before the job
# starts, when the dispatcher hasn't looked at the registry yet. The step
# summary is written afterwards, so it can name the fixtures by team.
_SUMMARY: list[str] = []


def note(line: str) -> None:
    """Add a line to the run's step summary."""
    _SUMMARY.append(line)


def write_summary() -> None:
    """Flush the collected lines to the run's summary page.

    Best-effort: unset outside Actions, and a summary that fails to write must
    never fail a tick that dispatched correctly."""
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path or not _SUMMARY:
        return
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(_SUMMARY) + '\n')
    except Exception as e:
        print(f'[dispatcher] WARNING: could not write step summary: {e}')


def active_match_ids() -> set:
    """
    Return match IDs that already have a match_bot run queued or in progress.

    'queued' matters as much as 'in_progress'. A run this dispatcher just
    triggered sits queued until a runner picks it up, which can take minutes.
    Meanwhile the external cron and GitHub's own schedule both fire every 15
    minutes, and the concurrency group queues those ticks rather than dropping
    them — so two ticks landing inside one fixture's 15-minute dispatch window
    is routine. Counting only in_progress runs would leave the second tick
    blind to the worker the first one started, and it would dispatch a
    duplicate: two workers on one match means two early cards and two final
    ones, with nothing downstream to deduplicate them.
    """
    ids = set()
    for status in ('in_progress', 'queued'):
        resp = requests.get(
            f'{API}/repos/{REPO}/actions/workflows/match_bot.yml/runs',
            params={'status': status, 'per_page': 50},
            headers=HEADS,
            timeout=10,
        )
        resp.raise_for_status()
        for run in resp.json().get('workflow_runs', []):
            name = run.get('name', '')
            if name.startswith('match-'):
                # "match-54493172 · Rayo Vallecano vs Deportivo" -> "54493172"
                ids.add(name[len('match-'):].split(' ', 1)[0])
    return ids


def trigger_worker(match_id: str, label: str = ''):
    resp = requests.post(
        f'{API}/repos/{REPO}/actions/workflows/match_bot.yml/dispatches',
        headers=HEADS,
        json={'ref': 'master',
              'inputs': {'match_id': match_id, 'label': label}},
        timeout=10,
    )
    if resp.status_code == 204:
        print(f'[dispatcher] Triggered worker for match {match_id}')
    else:
        print(f'[dispatcher] ERROR triggering {match_id}: {resp.status_code} {resp.text}')
        note(f'- ❌ **{label or match_id}** — could not start its worker '
             f'({resp.status_code})')
        write_summary()   # the tick dies here, so flush what it managed first
        sys.exit(1)


def _bot_runs(status: str) -> list:
    resp = requests.get(
        f'{API}/repos/{REPO}/actions/workflows/telegram_bot.yml/runs',
        params={'status': status, 'per_page': 10},
        headers=HEADS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('workflow_runs', [])


def telegram_bot_run() -> dict | None:
    """The bot run currently holding the token, or None."""
    runs = _bot_runs('in_progress')
    return runs[0] if runs else None


def telegram_bot_running() -> bool:
    """True if a telegram-bot run is queued or in progress."""
    return bool(_bot_runs('in_progress') or _bot_runs('queued'))


def _bot_needs_handoff(run: dict) -> bool:
    """Is this bot run close enough to the end of its job time to be relieved?"""
    started = run.get('run_started_at') or run.get('created_at')
    try:
        began = datetime.fromisoformat(str(started).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        # An unreadable timestamp is no reason to disturb a working bot: the
        # job's own timeout still ends it, and the next tick still restarts it.
        return False
    alive = (datetime.now(timezone.utc) - began).total_seconds()
    return alive >= BOT_HANDOFF_AFTER_SECS


def _dispatch_bot(reason: str) -> None:
    resp = requests.post(
        f'{API}/repos/{REPO}/actions/workflows/telegram_bot.yml/dispatches',
        headers=HEADS,
        json={'ref': 'master'},
        timeout=10,
    )
    if resp.status_code == 204:
        print(f'[dispatcher] Triggered Telegram bot — {reason}')
    else:
        print(f'[dispatcher] WARNING: could not trigger Telegram bot: '
              f'{resp.status_code} {resp.text}')


def ensure_telegram_bot(reason: str = 'a match worker is active'):
    """Start the photo-intake Telegram bot if it isn't already running.

    Also relieves one that is nearly out of job time. A hosted-runner job cannot
    outlive six hours, and any full Saturday of fixtures does — yesterday's card
    was one unbroken 10.6 hours of worker activity — so the bot holding the
    token is guaranteed to be cut mid-matchday.

    The concurrency group in telegram_bot.yml makes the relief cheap: a run
    dispatched while another is in progress does not run alongside it and is not
    refused, it waits as *pending* and starts the moment the incumbent exits. So
    the successor is queued shortly before the watchdog's own deadline and the
    token changes hands in about a minute, rather than the chat going quiet
    until a later tick notices nobody is there.

    Only ever called when the bot is wanted — a worker is active, or a message
    is waiting — so a bot on a quiet night is never handed off, just stopped.

    Best-effort throughout: a bot failure must never block match dispatching.
    """
    try:
        run = telegram_bot_run()
        if run is not None:
            if not _bot_needs_handoff(run):
                print('[dispatcher] Telegram bot already running')
            elif _bot_runs('queued'):
                print('[dispatcher] Telegram bot successor already queued')
            else:
                print('[dispatcher] Telegram bot is out of job time — queueing '
                      'its successor behind it')
                note('- 💬 **Telegram** — bot near its 6-hour job limit, '
                     'successor queued to take over')
                _dispatch_bot('handing over from a run that is out of job time')
            return

        if _bot_runs('queued'):
            print('[dispatcher] Telegram bot already queued')
            return

        _dispatch_bot(reason)
    except Exception as e:
        print(f'[dispatcher] WARNING: Telegram bot check failed: {e}')


def telegram_message_waiting() -> bool:
    """Is there an unread message sitting in the bot's update queue?

    This is what makes /card work when nothing is playing. The bot only exists
    while an Actions job holds it, so a message sent to a stopped bot goes
    nowhere until something starts one — and the bot cannot start itself,
    because it isn't running to hear the request. The dispatcher is already
    awake every fifteen minutes, so it does the listening.

    Two things make the peek safe:

      * No `offset`. Telegram only confirms (discards) updates when getUpdates
        is called with an offset above their id, so the default read leaves the
        queue exactly as it found it and the bot still receives the message.
        A negative offset would *not* be safe — that form forgets everything
        before the update it returns.
      * The caller must have established that no bot is running. Two pollers on
        one token fight over the same queue, and the live bot would start
        losing updates to this one.

    Any pending message counts, not just /card: a photo sent to a stopped bot
    deserves an answer just as much, and the bot's own handlers decide what to
    do with whatever it turns out to be.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        return False
    try:
        resp = requests.get(
            f'https://api.telegram.org/bot{token}/getUpdates',
            params={'limit': 1, 'timeout': 0},
            timeout=15,
        )
        resp.raise_for_status()
        return bool(resp.json().get('result'))
    except Exception as e:
        print(f'[dispatcher] WARNING: could not check for Telegram messages: {e}')
        return False


def wake_telegram_bot_if_messaged():
    """Start the bot when someone has messaged it while it was down."""
    try:
        if telegram_bot_running():
            return
        if not telegram_message_waiting():
            return
        print('[dispatcher] A message is waiting for the Telegram bot')
        note('- 💬 **Telegram** — a message was waiting, starting the bot')
        ensure_telegram_bot('a message is waiting')
    except Exception as e:
        print(f'[dispatcher] WARNING: Telegram wake check failed: {e}')


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
            note(f'- ❌ carousel **{group}** failed to post — {e}')
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
    for group, entries in groups_in(registry).items():
        try:
            # Entries and not just the name: the marker lives under the group's
            # matchday, which is derived from its members' kickoffs.
            if not posted_marker(group, entries):
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


def _overdue_hours(entry: dict, now) -> float:
    """How long this fixture has been prunable. Negative means not yet."""
    try:
        kickoff = datetime.fromisoformat(str(entry['kickoff_utc']).replace('Z', '+00:00'))
    except (KeyError, ValueError):
        return 0.0
    deadline = kickoff + timedelta(hours=PRUNE_AFTER_HOURS)
    return (now - deadline).total_seconds() / 3600


def _daily_window(now) -> bool:
    """True on the four ticks inside the daily housekeeping hour."""
    return now.astimezone(PRUNE_TZ).hour == PRUNE_HOUR_LOCAL


def _prune_window_open(removable: list, now) -> bool:
    """
    Is this the tick that should do the daily tidy-up?

    True during the daily window, or when something has been waiting so long
    that the window was clearly missed. Nothing is stored between runs: both
    answers come from the clock and the fixtures already in hand.
    """
    if _daily_window(now):
        return True
    worst = max((_overdue_hours(e, now) for e, _ in removable), default=0.0)
    if worst >= PRUNE_CATCHUP_HOURS:
        print(f'[dispatcher] Prune window was missed — a fixture has been '
              f'finished for {worst:.0f}h, tidying up now')
        return True
    return False


def prune_finished_matches(registry: list, running: set) -> tuple[list[str], str] | None:
    """
    Drop fixtures that can no longer need anything, and write the trimmed file.

    Conservative on purpose: a fixture only goes once it is past every deadline
    the bot has, has no worker running, and isn't holding up a carousel. A
    fixture that failed to post is dropped too — six hours on, no process will
    ever pick it up again, and the failure was alerted at the time.

    Returns (paths, commit subject) for the caller to push, or None when there
    was nothing to prune. Pushing is the caller's job so the registry and the
    day's log travel in a single commit.
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
        return None

    # Everything above is a dry run until the window opens: the fixtures stay
    # in the file, and the only cost of waiting is a slightly longer registry.
    if not _prune_window_open(removed, now):
        local = now.astimezone(PRUNE_TZ)
        print(f'[dispatcher] {len(removed)} fixture(s) ready to prune — holding '
              f'until the {PRUNE_HOUR_LOCAL:02d}:00 pass '
              f'(now {local:%H:%M} {local.tzname()})')
        return None

    for entry, reason in removed:
        print(f'[dispatcher] Pruning {entry.get("match_id")} '
              f'({entry.get("home_team")} vs {entry.get("away_team")}) — {reason}')

    with open(MATCHES_FILE, 'w', encoding='utf-8') as f:
        json.dump(kept, f, indent=2, ensure_ascii=False)
        f.write('\n')

    lines = '\n'.join(f'- {e.get("home_team")} vs {e.get("away_team")} '
                      f'({e.get("kickoff_utc")})' for e, _ in removed)
    subject = (f'remove {len(removed)} finished '
               f'fixture{"s" if len(removed) != 1 else ""}\n\n{lines}\n')
    return ['matches.json'], subject


def update_logbook(now) -> tuple[list[str], str] | None:
    """
    Render the per-match logs into `logs/<day>.md`.

    Once a day, in the same window as the prune, so the two housekeeping jobs
    share one commit. A live match rewrites its log every minute, so doing this
    per tick would mean a commit every fifteen — far more churn than the
    fixture pruning this window was introduced to fix.

    Nothing is lost by waiting: the logs are on Cloudinary the moment they are
    written, and `python logbook.py` renders them locally whenever you want to
    read one before the daily pass.

    Imported lazily, like the carousel: a fault in the log renderer must never
    stop matches being dispatched. Logging is the least important thing here.
    """
    if not _daily_window(now):
        return None

    try:
        import logbook
    except Exception as e:
        print(f'[dispatcher] WARNING: logbook unavailable ({e})')
        return None

    try:
        changed, dropped = logbook.sync(REPO_ROOT)
    except Exception as e:
        print(f'[dispatcher] WARNING: could not update the logbook: {e}')
        return None

    if dropped:
        print(f'[dispatcher] Dropped {dropped} staged log(s) past retention')
    if not changed:
        print('[dispatcher] Logbook already current')
        return None

    print(f'[dispatcher] Logbook updated: {", ".join(changed)}')
    days = sorted({os.path.basename(p)[:-3] for p in changed
                   if not p.endswith('README.md')})
    return changed, f'update match log ({", ".join(days) or "index"})\n'


def _git(*args) -> bool:
    """Run a git command in the repo. Logs and returns False on failure."""
    result = subprocess.run(('git', '-C', REPO_ROOT) + args,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f'[dispatcher] git {" ".join(args)} failed: '
              f'{(result.stderr or result.stdout).strip()[:300]}')
        return False
    return True


def _push(paths: list[str], message: str) -> None:
    """
    Commit the given paths and push them to master.

    Best-effort: this runner is thrown away either way, so a failed push costs
    nothing but a retry on the next tick. Rebasing first handles the case where
    a fixture was added by hand since this run checked out.
    """
    if not _git('config', 'user.name', GIT_AUTHOR_NAME):
        return
    if not _git('config', 'user.email', GIT_AUTHOR_EMAIL):
        return
    if not _git('add', *paths):
        return
    if not _git('commit', '-m', message):
        print('[dispatcher] Nothing staged — the tree was already current')
        return
    # Pull anything pushed since checkout so the push can't be rejected for
    # being behind; if that fails, leave it — the next tick tries again.
    _git('pull', '--rebase', 'origin', 'master')
    if _git('push', 'origin', 'HEAD:master'):
        print(f'[dispatcher] Pushed to master: {", ".join(paths)}')


def main():
    with open(MATCHES_FILE, encoding='utf-8') as f:
        registry = json.load(f)

    now     = datetime.now(timezone.utc)
    running = active_match_ids()
    print(f'[dispatcher] {now.isoformat(timespec="seconds")}  active workers: {running or "none"}')

    note(f'### Dispatcher tick — {now:%Y-%m-%d %H:%M} UTC')
    note('')

    upcoming = []
    header_lines = len(_SUMMARY)
    fired_any = False
    for entry in registry:
        match_id = entry['match_id']
        home     = entry.get('home_team', '?')
        away     = entry.get('away_team', '?')

        if match_id in running:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — already running, skip')
            note(f'- 🟢 **{home} vs {away}** — worker already running')
            continue

        try:
            kickoff = datetime.fromisoformat(entry['kickoff_utc'].replace('Z', '+00:00'))
        except (KeyError, ValueError) as e:
            print(f'[dispatcher] Bad kickoff_utc for {match_id}: {e}')
            continue

        lead = (LINEUP_PRE_MATCH_WINDOW_SECS if entry.get('post_lineups')
                else PRE_MATCH_WINDOW_SECS)
        window_open = kickoff - timedelta(seconds=lead)
        fire_by     = window_open + timedelta(seconds=DISPATCH_LOOKAHEAD)

        if window_open <= now <= fire_by:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — window open, firing worker')
            trigger_worker(match_id, f'{home} vs {away}')
            note(f'- 🚀 **{home} vs {away}** — worker fired (kickoff '
                 f'{kickoff:%H:%M} UTC)')
            fired_any = True
        elif now < window_open:
            mins = int((window_open - now).total_seconds() / 60)
            print(f'[dispatcher] {match_id} ({home} vs {away}) — opens in ~{mins} min')
            upcoming.append((mins, f'{home} vs {away}'))
        else:
            print(f'[dispatcher] {match_id} ({home} vs {away}) — window passed, skip')

    if len(_SUMMARY) == header_lines:
        note('- 💤 Nothing to dispatch this tick.')
    if upcoming:
        soonest, label = min(upcoming)
        note('')
        note(f'Next up: **{label}**, worker opens in ~{soonest} min '
             f'({len(upcoming)} fixture(s) waiting).')

    # Keep the photo-intake Telegram bot up whenever any worker is active.
    # Its watchdog shuts it down once the last match worker finishes.
    if running or fired_any:
        ensure_telegram_bot()
    else:
        # Nothing is playing, so nothing would normally hold the bot open. If a
        # message is waiting for it — a /card, a stray photo — start it anyway.
        # Only in this branch: peeking at the queue while the bot is polling it
        # would take updates out of its mouth.
        wake_telegram_bot_if_messaged()

    # Publish any carousel group that is now complete. Runs after dispatching so
    # a slow or failing post can never delay a worker for a kicking-off match.
    publish_carousel_groups(registry)

    # Then tidy the registry. After publishing, so a group that just went out
    # stops holding its fixtures back.
    pending = [c for c in (prune_finished_matches(registry, running),
                           update_logbook(now)) if c]

    # One commit for both: two pushes a tick would race each other for no gain,
    # and the registry and the log describe the same fifteen minutes anyway.
    if pending:
        paths = [p for c in pending for p in c[0]]
        subjects = [c[1] for c in pending]
        message = ('chore: ' + subjects[0] if len(subjects) == 1
                   else 'chore: update fixtures and match log\n\n'
                        + '\n\n'.join(s.strip() for s in subjects) + '\n')
        _push(paths, message)
        note('')
        note(f'Housekeeping: {", ".join(s.strip().splitlines()[0] for s in subjects)}.')

    write_summary()


if __name__ == '__main__':
    main()
