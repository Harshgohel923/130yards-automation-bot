# validate_matches.py
"""
Pre-flight check for matches.json — run it after adding fixtures.

For every match entry it:
    1. normalizes home_team / away_team to the official display name
       ('Man Utd' → 'Manchester United'), writing the fix back into
       matches.json so TheSportsDB, the scraper and the logo site all
       agree on one spelling
    2. confirms the crest actually exists on football-logos.cc
    3. fetches the crest to Cloudinary so it's ready before kickoff
       (skipped for crests already uploaded)

Exits non-zero if any team can't be resolved or any crest is missing,
printing did-you-mean hints so the entry can be corrected.

Usage:
    python validate_matches.py             # normalize + verify + upload
    python validate_matches.py --check     # dry run: report only, change nothing
    python validate_matches.py --no-upload # normalize + verify, skip Cloudinary
"""

import argparse
import json
import os
import sys

import requests

from config import get_crest_url
from logo_fetch import (COMPETITION_DISPLAY, COMPETITIONS, _scrape_hash,
                        fetch_competition_logo, fetch_logo,
                        normalize_team_name, resolve_competition, resolve_team)

MATCHES_FILE = 'matches.json'


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

    problems: list[str] = []
    renames: list[str] = []
    crest_cache: dict[str, str | None] = {}
    changed = False

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

    if renames:
        print('\nNormalized names:' if not args.check else '\nWould normalize:')
        for r in renames:
            print(f'  {r}')

    if changed:
        # Write-then-rename so a crash can never truncate the fixture file.
        tmp = MATCHES_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(matches, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, MATCHES_FILE)
        print(f'\nUpdated {MATCHES_FILE} with normalized names.')

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
