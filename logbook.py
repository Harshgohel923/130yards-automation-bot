# logbook.py
"""
Renders the per-match logs written by `matchlog` into browsable Markdown.

Run by the dispatcher, which is the only process that can safely commit: it is
serialised by its workflow `concurrency` group and already pushes matches.json,
so it can add the day's log to the same commit without racing anyone.

One file per day, `logs/<YYYY-MM-DD>.md`, dated and timed in German local time
— the log is read by a person in Germany, not by a machine in UTC. Storage
stays UTC; only the rendering is local.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.api
import requests

from matchlog import DISPLAY_TZ, FOLDER, _log_url

# Logs older than this are dropped from Cloudinary. By then the Markdown is
# committed to master, which is permanent and free — this only clears the
# staging copy so the bucket doesn't grow without bound.
RETAIN_DAYS = 14

LEVEL_MARK = {'info': '  ', 'warn': '⚠️', 'error': '❌'}


# ── Collecting ────────────────────────────────────────────────────────────────

def collect() -> list[dict]:
    """Every match log currently staged on Cloudinary."""
    try:
        listing = cloudinary.api.resources(
            type='upload', resource_type='raw',
            prefix=f'{FOLDER}/', max_results=500,
        )
    except Exception as e:
        print(f'[logbook] Could not list logs: {e}')
        return []

    docs = []
    for resource in listing.get('resources', []):
        url = resource.get('secure_url', '')
        try:
            response = requests.get(url, params={'_': int(datetime.now().timestamp())},
                                    timeout=15)
            response.raise_for_status()
            doc = response.json()
        except Exception as e:
            print(f'[logbook] Could not read {url}: {e}')
            continue
        if isinstance(doc, dict) and doc.get('match_id'):
            docs.append(doc)
    return docs


def _local(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).astimezone(DISPLAY_TZ)
    except Exception:
        return None


def _day_of(doc: dict) -> str | None:
    """The German calendar day a match belongs to — its kickoff, or failing
    that its first recorded event."""
    when = _local(str(doc.get('kickoff_utc') or ''))
    if when is None:
        events = doc.get('events') or []
        when = _local(events[0]['ts']) if events else None
    return when.strftime('%Y-%m-%d') if when else None


# ── Rendering ─────────────────────────────────────────────────────────────────

def _sort_key(doc: dict):
    when = _local(str(doc.get('kickoff_utc') or ''))
    return (when is None, when or datetime.max.replace(tzinfo=timezone.utc))


def render_match(doc: dict) -> str:
    home = doc.get('home_team', '?')
    away = doc.get('away_team', '?')
    kickoff = _local(str(doc.get('kickoff_utc') or ''))

    bits = [f"**{kickoff.strftime('%H:%M')}**" if kickoff else '**??:??**']
    if doc.get('competition'):
        bits.append(str(doc['competition']))
    if doc.get('group'):
        bits.append(f"carousel group `{doc['group']}`")
    if (doc.get('runs') or 1) > 1:
        bits.append(f"{doc['runs']} worker runs")
    bits.append(f"id `{doc.get('match_id')}`")

    lines = [f'## {home} vs {away}', '', ' · '.join(bits), '', '```']
    for event in doc.get('events') or []:
        when = _local(str(event.get('ts', '')))
        stamp = when.strftime('%H:%M:%S') if when else '  --:--'
        mark = LEVEL_MARK.get(event.get('level', 'info'), '  ')
        name = str(event.get('event', ''))
        detail = str(event.get('detail', '') or '')
        # Detail on its own continuation lines keeps the event column readable
        # when a traceback lands in it.
        first, *rest = (detail.splitlines() or [''])
        lines.append(f'{stamp} {mark} {name:<26} {first}'.rstrip())
        for extra in rest:
            lines.append(f'{"":>10}   {"":<26} {extra}'.rstrip())
    lines += ['```', '']
    return '\n'.join(lines)


def render_day(day: str, docs: list[dict]) -> str:
    docs = sorted(docs, key=_sort_key)
    events = [e for d in docs for e in (d.get('events') or [])]
    errors = sum(1 for e in events if e.get('level') == 'error')
    warns = sum(1 for e in events if e.get('level') == 'warn')
    posts = sum(1 for e in events if str(e.get('event', '')).startswith('posted'))

    heading = datetime.strptime(day, '%Y-%m-%d').strftime('%A, %-d %B %Y')
    sample = _local(str(docs[0].get('kickoff_utc') or '')) if docs else None
    zone = sample.tzname() if sample else 'CET/CEST'

    summary = [f'{len(docs)} match{"es" if len(docs) != 1 else ""}',
               f'{posts} post{"s" if posts != 1 else ""}']
    if warns:
        summary.append(f'{warns} warning{"s" if warns != 1 else ""}')
    if errors:
        summary.append(f'{errors} error{"s" if errors != 1 else ""}')

    out = [
        f'# Match log — {heading}',
        '',
        f'All times German local ({zone}). Written automatically by the '
        f'dispatcher; do not edit by hand.',
        '',
        f'**{" · ".join(summary)}**',
        '',
        '---',
        '',
    ]
    for doc in docs:
        out.append(render_match(doc))
    return '\n'.join(out).rstrip() + '\n'


def render_index(days: list[str], repo_root: str) -> str:
    """Entry point for someone browsing the folder: newest day first."""
    out = ['# Match logs', '',
           'One file per day, in German local time. Newest first.', '']
    for day in sorted(days, reverse=True):
        pretty = datetime.strptime(day, '%Y-%m-%d').strftime('%A, %-d %B %Y')
        out.append(f'- [{pretty}]({day}.md)')
    return '\n'.join(out) + '\n'


# ── Writing into the repo ─────────────────────────────────────────────────────

def sync(repo_root: str) -> tuple[list[str], int]:
    """
    Render every staged log into `logs/`, then drop the ones long since
    committed. One collection pass serves both.

    Returns (changed repo-relative paths, staged logs dropped) so the
    dispatcher only commits when there is news.
    """
    docs = collect()
    changed = write(repo_root, docs)
    dropped = prune_remote(docs) if changed is not None else 0
    return changed, dropped


def write(repo_root: str, docs: list[dict] | None = None) -> list[str]:
    """
    Render every staged log into `logs/`. Returns the repo-relative paths that
    actually changed, so the dispatcher only commits when there is news.
    """
    docs = collect() if docs is None else docs
    if not docs:
        return []

    by_day: dict[str, list[dict]] = {}
    for doc in docs:
        day = _day_of(doc)
        if day:
            by_day.setdefault(day, []).append(doc)
    if not by_day:
        return []

    log_dir = os.path.join(repo_root, FOLDER)
    os.makedirs(log_dir, exist_ok=True)

    changed = []
    for day, day_docs in by_day.items():
        path = os.path.join(log_dir, f'{day}.md')
        if _write_if_changed(path, render_day(day, day_docs)):
            changed.append(f'{FOLDER}/{day}.md')

    existing = sorted(f[:-3] for f in os.listdir(log_dir)
                      if f.endswith('.md') and f != 'README.md')
    index_path = os.path.join(log_dir, 'README.md')
    if _write_if_changed(index_path, render_index(existing, repo_root)):
        changed.append(f'{FOLDER}/README.md')

    return changed


def _write_if_changed(path: str, content: str) -> bool:
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == content:
                return False
    except FileNotFoundError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True


def _cli() -> None:
    """
    Render the staged logs locally, without committing anything.

    The dispatcher only writes the logbook once a day, so this is how you read
    a match's log before then — the events are on Cloudinary the moment they
    happen, this just renders them.

        python logbook.py            # write logs/<day>.md for every staged log
        python logbook.py --print    # print today's to the terminal instead
    """
    import sys

    docs = collect()
    if not docs:
        print('No staged match logs.')
        return

    if '--print' in sys.argv:
        by_day: dict[str, list[dict]] = {}
        for doc in docs:
            day = _day_of(doc)
            if day:
                by_day.setdefault(day, []).append(doc)
        for day in sorted(by_day, reverse=True):
            print(render_day(day, by_day[day]))
        return

    changed = write(os.path.dirname(os.path.abspath(__file__)), docs)
    print('\n'.join(f'wrote {c}' for c in changed) if changed
          else 'Already up to date.')


def prune_remote(docs: list[dict] | None = None,
                 now: datetime | None = None) -> int:
    """
    Drop staged logs whose day is already committed and long past.

    Deliberately unhurried: the Markdown is the permanent copy, so this only
    reclaims staging space. Never touches a log younger than RETAIN_DAYS.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETAIN_DAYS)
    removed = 0
    for doc in (collect() if docs is None else docs):
        events = doc.get('events') or []
        last = _local(str(events[-1]['ts'])) if events else None
        if last is None or last > cutoff.astimezone(DISPLAY_TZ):
            continue
        try:
            cloudinary.uploader.destroy(f"{FOLDER}/{doc['match_id']}.json",
                                        resource_type='raw', invalidate=True)
            removed += 1
        except Exception as e:
            print(f"[logbook] Could not drop staged log {doc['match_id']}: {e}")
    return removed


if __name__ == "__main__":
    _cli()
