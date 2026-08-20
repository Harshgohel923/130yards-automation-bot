"""
matches.json is well-formed.

The registry is hand-edited and pushed straight to master, and the dispatcher
rewrites it unattended when it prunes. A malformed file breaks every worker for
every fixture, not just the entry that was mistyped, so it is worth checking on
the way in rather than at kickoff.

Structural only. validate_matches.py does the richer checks — crest coverage,
URL shapes, carousel sizes — but it needs Cloudinary credentials, so it stays a
local tool.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

REGISTRY = Path(__file__).parent.parent / 'matches.json'
REQUIRED = ('match_id', 'scraper_url', 'kickoff_utc', 'home_team', 'away_team')


@pytest.fixture(scope='module')
def registry():
    with open(REGISTRY, encoding='utf-8') as f:
        return json.load(f)


def test_is_a_list(registry):
    assert isinstance(registry, list)


def test_every_entry_is_an_object(registry):
    assert all(isinstance(e, dict) for e in registry)


def test_required_fields_present(registry):
    for entry in registry:
        missing = [k for k in REQUIRED if not entry.get(k)]
        assert not missing, f"{entry.get('match_id', '?')} is missing {missing}"


def test_match_ids_are_unique(registry):
    # A duplicate would have the dispatcher treat two fixtures as one.
    ids = [e['match_id'] for e in registry]
    assert len(ids) == len(set(ids)), f'duplicate match_id in {ids}'


def test_kickoffs_parse_as_utc(registry):
    # The dispatcher's window arithmetic and the worker's safety ceiling both
    # read this; an unparsable value silently skips the fixture.
    for entry in registry:
        raw = entry['kickoff_utc']
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        assert parsed.tzinfo is not None, f'{raw} has no timezone'


def test_scraper_url_carries_the_match_id(registry):
    # The worker fetches this URL; an id/URL mismatch would follow the wrong
    # match while every log line named the right one.
    for entry in registry:
        assert entry['match_id'] in entry['scraper_url'], \
            f"{entry['match_id']} is not in its scraper_url"
