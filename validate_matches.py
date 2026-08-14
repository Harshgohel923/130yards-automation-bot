# validate_matches.py
"""
Pre-flight check for matches.json — run it after adding fixtures.

Adding a fixture only takes its scraper_url: match_id, kickoff_utc, home_team,
away_team and competition are read off the match page and written back for you
to check. Anything you did fill in is left alone — this fills blanks, it does
not overwrite. So a new entry can be as short as:

    { "scraper_url": "https://m.allfootballapp.com/match/Main/x/54457613" }

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
import json
import os
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import requests

from config import get_crest_url
from football_scraper_dom import get_match_data
from logo_fetch import (COMPETITION_DISPLAY, COMPETITIONS, _scrape_hash,
                        fetch_competition_logo, fetch_logo,
                        normalize_team_name, resolve_competition, resolve_team)

MATCHES_FILE = 'matches.json'

REQUIRED_FIELDS = ('match_id', 'scraper_url', 'kickoff_utc', 'home_team', 'away_team')
SCRAPER_HOST = 'm.allfootballapp.com'

# Fields derivable from the match page, so only scraper_url has to be typed.
AUTO_FIELDS = ('match_id', 'kickoff_utc', 'home_team', 'away_team', 'competition')

# Key order for entries this script fills in, so a generated entry reads the
# same way as a hand-written one. Anything unlisted keeps its position at the
# end. Only reordered when a fixture is actually filled, to keep diffs quiet.
FIELD_ORDER = ('match_id', 'scraper_url', 'kickoff_utc', 'home_team', 'away_team',
               'competition', 'post_ht', 'post_ft_stats', 'knockout_match',
               'carousel_group', 'records')

# Instagram will not accept more slides than this in one post, and a carousel
# group is one slide per match.
MAX_CAROUSEL_MATCHES = 10


def _reorder(entry: dict) -> dict:
    """Same entry, keys in FIELD_ORDER, extras preserved after them."""
    ordered = {k: entry[k] for k in FIELD_ORDER if k in entry}
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    return ordered


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
            if host != SCRAPER_HOST:
                problems.append(
                    f"match {match_id}: scraper_url host is '{host}' — it must be "
                    f"'{SCRAPER_HOST}'. Only the mobile page carries the match "
                    f"data blob; any other host returns nothing. Keep the trailing "
                    f"id and rebuild the URL as "
                    f"https://{SCRAPER_HOST}/match/Main/<teams>/<id>.")
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

    # Fill blank fields from the match page first, so the checks below see a
    # complete entry and a new fixture only needs its scraper_url.
    filled, fill_problems, changed = _autofill(matches, write=not args.check)

    problems: list[str] = fill_problems + _check_structure(matches)
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
                known = ', '.join(sorted(COMPETITION_DISPLAY.values()))
                problems.append(f"match {match_id}: unknown competition '{comp}'. "
                                f"Known: {known}")
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
