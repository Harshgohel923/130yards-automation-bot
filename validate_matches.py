# validate_matches.py
"""
Pre-flight check for matches.json — run it after adding fixtures.

Adding a fixture only takes its scraper_url: match_id, kickoff_utc, home_team,
away_team and competition are read off the match page and written back for you
to check. Anything you did fill in is left alone — this fills blanks, it does
not overwrite. So a new entry can be as short as:

    { "scraper_url": "https://m.allfootballapp.com/match/Main/x/54457613" }

Either host works. The desktop site lists fixtures the mobile UI never shows,
so a match is often easier to find there; paste that URL and the mobile one is
computed for you (and vice versa — desktop_url is filled in either way):

    { "scraper_url": "https://www.allfootballapp.com/match/54457613" }

Fixtures are sorted by kick-off, earliest first, every time this runs, and
kickoff_local is refreshed to show that kick-off in German time — 'Sun 16 Aug
2026, 02:30 CEST' — and kickoff_ist the same kick-off in Indian time — 'Sun 16
Aug 2026, 06:00 IST'. Both are derived from kickoff_utc, which stays the only
kickoff the bot reads; editing either of them moves nothing.

Then it checks the file's structure — the mistakes that are easy to make when
hand-editing fixtures and that no amount of name-checking would catch:
    * required fields present
    * match_id unique across entries (it is the database primary key and the
      worker-registry key; a duplicate silently costs you one of the matches)
    * match_id agrees with the id at the end of scraper_url
    * kickoff_utc parses and is timezone-aware (a missing 'Z' yields a naive
      datetime and crashes the worker on its first comparison)
    * scraper_url points at the mobile host, the only one carrying match data

Then, for every match entry it:
    1. normalizes home_team / away_team to the official display name
       ('Man Utd' → 'Manchester United'), writing the fix back into
       matches.json so the scraper and the logo site agree on one spelling
    2. confirms the crest actually exists on football-logos.cc
    3. fetches the crest to Cloudinary so it's ready before kickoff
       (skipped for crests already uploaded)

Exits non-zero if the structure is wrong, a team can't be resolved or a crest
is missing, printing did-you-mean hints so the entry can be corrected.

Usage:
    python validate_matches.py             # normalize + verify + upload
    python validate_matches.py --check     # dry run: report only, change nothing
    python validate_matches.py --no-upload # normalize + verify, skip Cloudinary
"""

import argparse
import difflib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import requests

from config import IST_TZ, LOCAL_TZ, get_crest_url
from football_scraper_dom import get_match_data
from logo_fetch import (COMPETITION_DISPLAY, COMPETITIONS, _scrape_hash,
                        _slugify, fetch_competition_logo, fetch_logo,
                        normalize_team_name, resolve_competition, resolve_team)

MATCHES_FILE = 'matches.json'

REQUIRED_FIELDS = ('match_id', 'scraper_url', 'kickoff_utc', 'home_team', 'away_team')
SCRAPER_HOST = 'm.allfootballapp.com'

# The desktop site lists fixtures the mobile UI never surfaces, so a fixture is
# often easiest to find there. Either host may be pasted into scraper_url; the
# mobile one is written back, because only it carries the match data blob.
DESKTOP_HOSTS = ('www.allfootballapp.com', 'allfootballapp.com')

# Fields derivable from the match page, so only scraper_url has to be typed.
AUTO_FIELDS = ('match_id', 'kickoff_utc', 'home_team', 'away_team', 'competition')

# Key order for entries this script fills in, so a generated entry reads the
# same way as a hand-written one. Anything unlisted keeps its position at the
# end. Only reordered when a fixture is actually filled, to keep diffs quiet.
FIELD_ORDER = ('match_id', 'scraper_url', 'desktop_url', 'kickoff_utc',
               'kickoff_local', 'kickoff_ist', 'home_team', 'away_team',
               'competition', 'post_ht', 'post_ft_stats', 'post_lineups', 'lineups_first',
               'coaches', 'knockout_match', 'carousel_group', 'records')

# Instagram will not accept more slides than this in one post, and a carousel
# group is one slide per match.
MAX_CAROUSEL_MATCHES = 10


def _reorder(entry: dict) -> dict:
    """Same entry, keys in FIELD_ORDER, extras preserved after them."""
    ordered = {k: entry[k] for k in FIELD_ORDER if k in entry}
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    return ordered


def _match_id_in(url: str) -> str | None:
    """
    The match id inside either URL form.

    Mobile is /match/Main/<teams>/<id>, desktop is bare /match/<id>, so rather
    than pattern-match a shape that could change, take the last all-digit path
    segment. Both forms end in one and nothing else in either path is numeric.
    """
    segments = [s for s in urlparse(url).path.split('/') if s]
    for segment in reversed(segments):
        if segment.isdigit():
            return segment
    return None


def mobile_url(match_id: str, home: str | None = None, away: str | None = None) -> str:
    """
    The scrapeable URL for a match.

    The slug is decoration — the server resolves purely on the trailing id,
    verified against real, dummy and deliberately wrong slugs, all of which
    return the same match. It is built from the team names anyway so the URL
    stays readable when you check it.
    """
    slug = f'{_slugify(home)}-vs-{_slugify(away)}' if home and away else 'match'
    return f'https://{SCRAPER_HOST}/match/Main/{slug}/{match_id}'


def desktop_url(match_id: str) -> str:
    """The desktop page — same match, the host with the fuller fixture list."""
    return f'https://{DESKTOP_HOSTS[0]}/match/{match_id}'


def _normalize_urls(matches: list, write: bool) -> tuple[list[str], list[str], bool]:
    """
    Accept either host in `scraper_url`, and keep `desktop_url` beside it.

    Runs before autofill, which needs a URL it can actually scrape. A desktop
    URL carries no team names, so the mobile URL is built with a placeholder
    slug here and rewritten with the real names once autofill has them.

    Returns (notes, problems, changed).
    """
    notes: list[str] = []
    problems: list[str] = []
    changed = False

    for i, entry in enumerate(matches):
        if not isinstance(entry, dict):
            continue
        url = str(entry.get('scraper_url') or '').strip()
        if not url:
            continue                     # reported by the structure check

        label = entry.get('match_id') or f'entry #{i + 1}'
        host = (urlparse(url).hostname or '').lower()
        match_id = _match_id_in(url)

        if match_id is None:
            problems.append(
                f"{label}: no match id found in scraper_url '{url}'. Both forms "
                f"end in the numeric id — https://{SCRAPER_HOST}/match/Main/"
                f"<teams>/<id> or https://{DESKTOP_HOSTS[0]}/match/<id>.")
            continue

        if host in DESKTOP_HOSTS:
            entry['scraper_url'] = mobile_url(match_id,
                                              entry.get('home_team'),
                                              entry.get('away_team'))
            notes.append(f"{label}: desktop URL → {entry['scraper_url']}")
            changed = changed or write
        elif host != SCRAPER_HOST:
            continue                     # unknown host — the structure check reports it

        if not entry.get('desktop_url'):
            entry['desktop_url'] = desktop_url(match_id)
            changed = changed or write

        matches[i] = _reorder(entry)

    return notes, problems, changed


def _retitle_urls(matches: list, write: bool) -> bool:
    """
    Put the real team names into any URL still carrying the placeholder slug.

    Only touches URLs this script generated a moment ago, so a hand-written
    URL — however it spells its teams — is left exactly as typed.
    """
    changed = False
    for entry in matches:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get('scraper_url') or '')
        if '/match/Main/match/' not in url:
            continue
        match_id = _match_id_in(url)
        home, away = entry.get('home_team'), entry.get('away_team')
        if not (match_id and home and away):
            continue
        entry['scraper_url'] = mobile_url(match_id, home, away)
        changed = changed or write
    return changed


def _kickoff_in(kickoff_utc: str, tz) -> str | None:
    """
    'Sat 15 Aug 2026, 20:00 CEST' — the kickoff as someone in `tz` reads it.

    The zone abbreviation is the point of including it: CEST means summer time
    is on, CET means it is off, so the field answers the daylight-saving
    question by itself rather than leaving it to be worked out.
    """
    try:
        parsed = datetime.fromisoformat(str(kickoff_utc).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(tz).strftime('%a %-d %b %Y, %H:%M %Z')


def _refresh_local_times(matches: list, write: bool, field: str,
                        tz) -> tuple[list[str], bool]:
    """
    Keep `field` — `kickoff_local` or `kickoff_ist` — in step with `kickoff_utc`.

    Derived, always — `kickoff_utc` is the only kickoff the bot reads, and both
    readings are recomputed from it on every run. Editing them by hand moves
    nothing; the change is reported here and then overwritten, so they can't
    quietly disagree with the time the match is actually tracked at.
    """
    notes: list[str] = []
    changed = False

    for entry in matches:
        if not isinstance(entry, dict):
            continue
        local = _kickoff_in(entry.get('kickoff_utc'), tz)
        if local is None:
            continue                     # unparseable — the structure check has it
        previous = entry.get(field)
        if previous == local:
            continue
        entry[field] = local
        if previous is None:
            # A key added by assignment lands at the end of the entry; put it
            # back beside the other kickoffs so the entry still reads in order.
            ordered = _reorder(dict(entry))
            entry.clear()
            entry.update(ordered)
        changed = changed or write
        label = entry.get('match_id', '?')
        notes.append(f'match {label}: {previous} → {local}' if previous
                     else f'match {label}: {local}')

    return notes, changed


def _sort_by_kickoff(matches: list) -> bool:
    """
    Order the fixture list by kick-off, earliest first.

    Purely for reading: nothing downstream cares about order — the dispatcher
    scans the whole list every tick — but a file you hand-edit is easier to
    work with in the order the matches actually happen. A fixture whose
    kickoff can't be parsed sorts last rather than crashing the sort; the
    structure check reports it separately.
    """
    def key(entry):
        if not isinstance(entry, dict):
            return (2, '')
        try:
            parsed = datetime.fromisoformat(
                str(entry['kickoff_utc']).replace('Z', '+00:00'))
        except (KeyError, ValueError, TypeError):
            return (1, str(entry.get('match_id', '')))
        return (0, parsed.isoformat())

    ordered = sorted(matches, key=key)
    if ordered == matches:
        return False
    matches[:] = ordered
    return True


def _kickoff_from(match_sample: dict) -> str | None:
    """
    kickoff_utc from the scraper's date_utc + time_utc.

    Those two fields are genuinely UTC — verified against hand-entered
    fixtures, which they reproduce exactly. The trailing 'Z' is what makes
    the timestamp timezone-aware downstream, so it is never omitted.
    """
    date = str(match_sample.get('date_utc') or '').strip()
    time = str(match_sample.get('time_utc') or '').strip()
    if not date or not time:
        return None
    if len(time.split(':')) == 2:      # HH:MM → HH:MM:SS
        time += ':00'
    return f'{date}T{time}Z'


def _autofill(matches: list, write: bool) -> tuple[list[str], list[str], bool]:
    """
    Fill any AUTO_FIELDS left blank from the match page, so a new fixture only
    needs its scraper_url pasted in.

    Existing values are never overwritten — this fills gaps, it does not
    correct what you typed. Filled names go through the same normalization as
    hand-written ones, so 'Betis' still becomes 'Real Betis'.

    Returns (filled, problems, changed). Values are always applied in memory so
    the checks that follow see a complete entry; `write` decides whether the
    change is meant to reach disk.
    """
    filled: list[str] = []
    problems: list[str] = []
    changed = False

    for i, entry in enumerate(matches):
        if not isinstance(entry, dict):
            continue
        url = entry.get('scraper_url')
        if not url:
            continue                     # reported by the structure check
        missing = [f for f in AUTO_FIELDS if not entry.get(f)]
        if not missing:
            continue

        label = entry.get('match_id') or f'entry #{i + 1}'
        print(f'… {label}: looking up {", ".join(missing)} from the match page')
        try:
            data = get_match_data(url)
        except Exception as e:
            data = None
            print(f'[autofill] {url} failed: {e}')
        if not data:
            problems.append(f"{label}: could not read {url} to fill "
                            f"{', '.join(missing)} — check the URL, then re-run")
            continue

        ms = data.get('matchSample') or {}
        available = {
            'match_id':    str(ms.get('match_id') or '').strip() or None,
            'kickoff_utc': _kickoff_from(ms),
            'home_team':   (ms.get('team_A_name') or '').strip() or None,
            'away_team':   (ms.get('team_B_name') or '').strip() or None,
            'competition': (ms.get('competition_name') or '').strip() or None,
        }
        for field in missing:
            value = available.get(field)
            if not value:
                problems.append(f'{label}: the match page carries no {field} — '
                                f'fill it in by hand')
                continue
            entry[field] = value
            filled.append(f'{label}: {field} = {value!r}')
            changed = changed or write

        matches[i] = _reorder(entry)

    return filled, problems, changed


def _check_structure(matches: list) -> list[str]:
    """
    Structural problems in the fixture list, as a list of messages.

    These are all things the name/crest checks below would happily pass over,
    and each one costs a post at match time rather than failing loudly.
    """
    problems: list[str] = []

    if not isinstance(matches, list):
        return [f'{MATCHES_FILE} must contain a list of match objects']

    # Duplicate ids — reported once each, not once per copy.
    counts = Counter(e.get('match_id') for e in matches if isinstance(e, dict))
    for mid, n in counts.items():
        if mid is not None and n > 1:
            problems.append(
                f"match_id '{mid}' is used by {n} entries — ids must be unique. "
                f"It keys the database and the worker registry, so only one of "
                f"those matches would ever be posted.")

    for i, entry in enumerate(matches):
        if not isinstance(entry, dict):
            problems.append(f'entry #{i + 1} is not an object')
            continue

        match_id = entry.get('match_id', f'#{i + 1}')

        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            problems.append(f"match {match_id}: missing or empty {', '.join(missing)}")

        url = entry.get('scraper_url')
        if url:
            host = (urlparse(url).hostname or '').lower()
            if host in DESKTOP_HOSTS:
                # Conversion runs before this and rewrites anything usable, so a
                # desktop URL still here is one it couldn't read an id from —
                # already reported, and saying "wrong host" too would be wrong.
                pass
            elif host != SCRAPER_HOST:
                problems.append(
                    f"match {match_id}: scraper_url host is '{host}', which isn't "
                    f"an AllFootball match page. Paste either the mobile URL "
                    f"(https://{SCRAPER_HOST}/match/Main/<teams>/<id>) or the "
                    f"desktop one (https://{DESKTOP_HOSTS[0]}/match/<id>) — the "
                    f"desktop form is converted for you.")
            url_id = urlparse(url).path.rstrip('/').rsplit('/', 1)[-1]
            if entry.get('match_id') and url_id != entry['match_id']:
                problems.append(
                    f"match {match_id}: match_id does not match the id in "
                    f"scraper_url ('{url_id}'). One of the two is wrong — the "
                    f"URL decides which match is actually scraped.")

        if 'post_ft_stats' in entry and not isinstance(entry['post_ft_stats'], bool):
            problems.append(
                f"match {match_id}: post_ft_stats must be true or false, got "
                f"{entry['post_ft_stats']!r}.")

        if 'post_lineups' in entry and not isinstance(entry['post_lineups'], bool):
            problems.append(
                f"match {match_id}: post_lineups must be true or false, got "
                f"{entry['post_lineups']!r}.")

        if 'lineups_first' in entry:
            first = entry['lineups_first']
            if not isinstance(first, str) or first.strip().lower() not in ('home', 'away'):
                problems.append(
                    f"match {match_id}: lineups_first must be \"home\" or "
                    f"\"away\", got {first!r}. It decides which team's XI is "
                    f"slide one.")
            elif not entry.get('post_lineups'):
                problems.append(
                    f"match {match_id}: lineups_first is set but post_lineups "
                    f"is not, so no line-up post would be made. Add "
                    f"\"post_lineups\": true, or drop lineups_first.")

        if 'coaches' in entry:
            coaches = entry['coaches']
            if not isinstance(coaches, dict):
                problems.append(
                    f"match {match_id}: coaches must be an object like "
                    f'{{"home": "M. Arteta", "away": "P. Guardiola"}}, got '
                    f"{coaches!r}.")
            else:
                stray = [k for k in coaches if k not in ('home', 'away')]
                if stray:
                    problems.append(
                        f"match {match_id}: coaches has unknown key(s) "
                        f"{', '.join(map(repr, stray))} — only \"home\" and "
                        f"\"away\" are used.")
                for role, name in coaches.items():
                    if role in ('home', 'away') and not isinstance(name, str):
                        problems.append(
                            f"match {match_id}: coaches.{role} must be a name, "
                            f"got {name!r}.")

        if 'carousel_group' in entry:
            group = entry['carousel_group']
            if group is not None and not isinstance(group, str):
                problems.append(
                    f"match {match_id}: carousel_group must be a string id (or "
                    f"omitted), got {group!r}.")
            elif isinstance(group, str) and not group.strip():
                problems.append(
                    f"match {match_id}: carousel_group is empty. Remove the "
                    f"field to post this match on its own, or give the group a "
                    f"name shared with the other matches it posts with.")

        kickoff = entry.get('kickoff_utc')
        if kickoff:
            # Parsed exactly as main.py does, so this catches what it would hit.
            try:
                parsed = datetime.fromisoformat(str(kickoff).replace('Z', '+00:00'))
            except ValueError as e:
                problems.append(f"match {match_id}: kickoff_utc '{kickoff}' is not a "
                                f"valid ISO-8601 timestamp ({e})")
            else:
                if parsed.tzinfo is None:
                    problems.append(
                        f"match {match_id}: kickoff_utc '{kickoff}' has no timezone — "
                        f"add a trailing 'Z' ('{kickoff}Z'). Without it the worker "
                        f"crashes comparing a naive time against UTC, and the match "
                        f"goes uncovered.")

    problems += _check_carousel_sizes(matches)
    return problems


def _carousel_groups(matches: list) -> dict[str, list[dict]]:
    """{carousel_group: entries}, mirroring carousel.groups_in()."""
    groups: dict[str, list[dict]] = {}
    for entry in matches:
        if not isinstance(entry, dict):
            continue
        group = entry.get('carousel_group')
        if isinstance(group, str) and group.strip():
            groups.setdefault(group.strip(), []).append(entry)
    return groups


def _check_carousel_sizes(matches: list) -> list[str]:
    """
    Reject a group too big for one Instagram post.

    Caught here, on the day you edit the fixture list, rather than at full time
    when the group is ready to go out and there is nothing to be done about it.
    """
    problems = []
    for group, entries in _carousel_groups(matches).items():
        if len(entries) > MAX_CAROUSEL_MATCHES:
            ids = ', '.join(str(e.get('match_id', '?')) for e in entries)
            problems.append(
                f"carousel_group '{group}' has {len(entries)} matches — "
                f"Instagram allows at most {MAX_CAROUSEL_MATCHES} slides in one "
                f"post, and a group is one slide per match. Split it into two "
                f"groups. Matches: {ids}")
    return problems


def _carousel_notes(matches: list) -> list[str]:
    """Advisory notes about grouping — worth saying, not worth failing over."""
    notes = []
    for group, entries in sorted(_carousel_groups(matches).items()):
        ids = ', '.join(str(e.get('match_id', '?')) for e in entries)
        if len(entries) == 1:
            notes.append(
                f"carousel_group '{group}' has only one match ({ids}) — it will "
                f"post as a single image, not a carousel. Check the group id if "
                f"that isn't what you meant.")
        else:
            notes.append(f"carousel_group '{group}': {len(entries)} matches ({ids})")

        stats_too = [str(e.get('match_id', '?')) for e in entries
                     if e.get('post_ft_stats')]
        if stats_too:
            notes.append(
                f"  ↳ post_ft_stats is set on {', '.join(stats_too)}, but grouped "
                f"matches post one scorecard each — no stats page is rendered "
                f"for them.")
    return notes


def _verify_crest(official: str, upload: bool, cache: dict[str, str | None]) -> str | None:
    """
    Return None if the crest is available, else an error message.
    Results are cached per team so repeated fixtures don't re-check.
    """
    if official in cache:
        return cache[official]

    error = None
    try:
        if upload:
            fetch_logo(official)   # idempotent: skips crests already on Cloudinary
        else:
            country, site_slug, _, _ = resolve_team(official)
            _scrape_hash(country, site_slug)   # proves a 3000px download exists
    except (LookupError, RuntimeError, requests.RequestException) as e:
        error = str(e)

    cache[official] = error
    return error


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate team names and crests in matches.json.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true',
                       help='dry run: report problems, do not modify matches.json or upload')
    group.add_argument('--no-upload', action='store_true',
                       help='normalize and verify the crest exists, but skip Cloudinary upload')
    args = parser.parse_args()

    try:
        with open(MATCHES_FILE) as f:
            matches = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'✖ {MATCHES_FILE} could not be read: {e}')
        return 1

    # Settle the URLs before anything reads them: a desktop URL becomes its
    # mobile twin here, so autofill has a page it can actually scrape.
    converted, url_problems, changed = _normalize_urls(matches, write=not args.check)

    # Fill blank fields from the match page next, so the checks below see a
    # complete entry and a new fixture only needs its scraper_url.
    filled, fill_problems, filled_changed = _autofill(matches, write=not args.check)
    changed = changed or filled_changed

    problems: list[str] = url_problems + fill_problems + _check_structure(matches)
    renames: list[str] = []
    crest_cache: dict[str, str | None] = {}

    # Structural faults make the per-entry checks unreliable (and a malformed
    # entry can't be name-checked at all), so report them on their own.
    if problems:
        print(f'\n✖ {len(problems)} structural problem(s) in {MATCHES_FILE}:')
        for p in problems:
            print(f'  - {p}')
        print('\nFix these first, then re-run — team names and crests were not checked.')
        return 1

    for entry in matches:
        match_id = entry.get('match_id', '?')

        for field in ('home_team', 'away_team'):
            raw = entry.get(field)
            if not raw:
                problems.append(f"match {match_id}: '{field}' is missing or empty")
                continue

            try:
                official = normalize_team_name(raw)
            except LookupError as e:
                # Not on the source site — a manually uploaded crest
                # (logo_fetch.py --local) still counts as valid.
                if get_crest_url(raw, alert=False):
                    print(f'✔ match {match_id}: {field} = {raw} (manual crest on Cloudinary)')
                    continue
                problems.append(f"match {match_id}: {field} — {e}")
                continue

            if official != raw:
                renames.append(f"match {match_id}: {field} '{raw}' → '{official}'")
                if not args.check:
                    entry[field] = official
                    changed = True

            error = _verify_crest(official, upload=not (args.check or args.no_upload),
                                  cache=crest_cache)
            if error:
                problems.append(f"match {match_id}: no crest for '{official}' — {error}")
            else:
                print(f'✔ match {match_id}: {field} = {official} (crest OK)')

        # Optional competition field — normalized the same way; its logo is
        # fetched up front so the card never falls back mid-match.
        comp = entry.get('competition')
        if comp:
            key = resolve_competition(comp)
            if key is None:
                # No entry for this competition — but a badge uploaded by hand
                # lands at the slugified name, and get_competition_logo_url
                # looks there, so an uploaded badge means the name is
                # deliberate rather than a typo. Accept it as typed.
                from config import get_brand_logo_url, get_competition_logo_url
                if get_competition_logo_url(comp, alert=False) != get_brand_logo_url():
                    print(f'✔ match {match_id}: competition = {comp} (badge uploaded by hand)')
                    continue
                close = difflib.get_close_matches(
                    comp, sorted(COMPETITION_DISPLAY.values()), n=5, cutoff=0.6)
                hint = (f"Did you mean: {', '.join(close)}?"
                        if close else
                        f"Known: {', '.join(sorted(COMPETITION_DISPLAY.values()))}.")
                problems.append(
                    f"match {match_id}: '{comp}' isn't a competition the bot "
                    f"knows, and no badge has been uploaded for it.\n"
                    f"    {hint}\n"
                    f"    If the name is right, supply a badge for it:\n"
                    f"      python logo_fetch.py --local <file.png> "
                    f"--competition \"{comp}\"")
                continue
            official = COMPETITION_DISPLAY[key]
            if official != comp:
                renames.append(f"match {match_id}: competition '{comp}' → '{official}'")
                if not args.check:
                    entry['competition'] = official
                    changed = True
            if args.check or args.no_upload:
                print(f'✔ match {match_id}: competition = {official}')
            elif COMPETITIONS[key] is None:
                # No site source (friendly) — the logo must already be on
                # Cloudinary from a --local upload.
                from config import get_brand_logo_url, get_competition_logo_url
                if get_competition_logo_url(official, alert=False) == get_brand_logo_url():
                    problems.append(
                        f"match {match_id}: no logo uploaded for '{official}' — "
                        f"upload one: python logo_fetch.py --local <file.png> "
                        f"--competition \"{official}\"")
                else:
                    print(f'✔ match {match_id}: competition = {official} (logo OK)')
            else:
                try:
                    fetch_competition_logo(official)
                    print(f'✔ match {match_id}: competition = {official} (logo OK)')
                except (LookupError, RuntimeError, requests.RequestException) as e:
                    problems.append(f"match {match_id}: no logo for competition "
                                    f"'{official}' — {e}")

    if converted:
        print('\nConverted from the desktop URL:' if not args.check
              else '\nWould convert from the desktop URL:')
        for c in converted:
            # Re-read: the slug was filled in with the real names after autofill.
            mid = c.split(':', 1)[0].strip()
            final = next((e['scraper_url'] for e in matches
                          if isinstance(e, dict) and str(e.get('match_id')) == mid
                          and e.get('scraper_url')), None)
            print(f'  {c.split(" → ")[0]} → {final}' if final else f'  {c}')
        print('  ↳ check these point at the right match before pushing.')

    if filled:
        print('\nFilled from the match page:' if not args.check
              else '\nWould fill from the match page:')
        for f in filled:
            print(f'  {f}')
        print('  ↳ verify these before pushing.')

    if renames:
        print('\nNormalized names:' if not args.check else '\nWould normalize:')
        for r in renames:
            print(f'  {r}')

    notes = _carousel_notes(matches)
    if notes:
        print('\nCarousel groups:')
        for n in notes:
            print(f'  {n}')

    # Now the teams have been normalized, a URL built from a desktop link can
    # carry their official names rather than the placeholder — or the scraper's
    # spelling, which is what it would have picked up any earlier than this.
    changed = _retitle_urls(matches, write=not args.check) or changed

    for field, tz, label in (('kickoff_local', LOCAL_TZ, 'German'),
                             ('kickoff_ist', IST_TZ, 'Indian')):
        local_notes, local_changed = _refresh_local_times(
            matches, write=not args.check, field=field, tz=tz)
        changed = changed or local_changed
        if local_notes:
            print(f'\nKick-off in {label} time:' if not args.check
                  else f'\nWould set the {label} kick-off time:')
            for n in local_notes:
                print(f'  {n}')

    # Last, so the file is written in the order it will be read. Only after the
    # structure check has passed, since that is what guarantees every
    # kickoff_utc actually parses.
    if _sort_by_kickoff(matches):
        print('\nSorted fixtures by kick-off time.' if not args.check
              else '\nWould sort fixtures by kick-off time.')
        changed = changed or not args.check

    if changed:
        # Write-then-rename so a crash can never truncate the fixture file.
        tmp = MATCHES_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(matches, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, MATCHES_FILE)
        print(f'\nUpdated {MATCHES_FILE}.')

    if problems:
        print(f'\n✖ {len(problems)} problem(s):')
        for p in problems:
            print(f'  - {p}')
        # Only the guide that applies: a run that failed purely on competitions
        # shouldn't be answered with four steps about team crests.
        if any("isn't a competition" in p or 'competition' in p.split(':', 1)[-1][:40]
               for p in problems):
            print(
                "\nHow to fix an unresolved competition:\n"
                "  1. Check the spelling against the known names listed above, "
                "and use one of those if it matches.\n"
                "  2. New competition? Supply its badge yourself — any PNG, "
                "ideally square and transparent:\n"
                "     python logo_fetch.py --local badge.png --competition "
                "\"Saudi PL\"  — then re-run this.\n"
                "     The name you pass is the name to keep in matches.json; "
                "the badge is found by it from then on.\n"
                "  3. Want it fetched automatically instead, and the site "
                "carries it? Add it to COMPETITIONS in logo_fetch.py, then "
                "push before match day."
            )
        if not all("isn't a competition" in p for p in problems):
            print(
                "\nHow to fix an unresolved team:\n"
                "  1. Find it on football-logos.cc — the page URL is "
                "football-logos.cc/<country>/<slug>/\n"
                "     (the did-you-mean hints above are real slugs from that site).\n"
                "  2. Easiest: use the slug's spelling as the team name in "
                "matches.json ('jeju-sk-fc' → \"Jeju SK FC\") and re-run this.\n"
                "  3. Want a different name on the card? Add the team to "
                "SITE_OVERRIDES (and, if needed, DISPLAY_NAMES / NICKNAMES) in "
                "logo_fetch.py, re-run this, then push before match day.\n"
                "  4. Team not on the site at all? Upload your own PNG:\n"
                "     python logo_fetch.py --local crest.png \"Team Name\"  "
                "— then re-run this."
            )
        return 1

    print(f'\n✔ All {len(matches)} match(es) valid.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
