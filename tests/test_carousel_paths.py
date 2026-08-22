"""
Where a carousel group keeps its state on Cloudinary.

Group ids are labels a person types into matches.json — '17:00', 'saturday' —
and they get reused every week. On 2026-08-22 the '17:00' group never posted:
a POSTED marker written by a different '17:00' group six days earlier still sat
at carousel/17:00/POSTED.json, and publish_group() checks that marker first and
returns quietly. Three finished matches sat on Cloudinary all evening with no
error anywhere.

So the folder has to be unique per *use* of a name, not per name. These tests
cover the derivation only — the Cloudinary calls around it stay out of the
suite, like every other network path in this codebase.
"""

import json

import pytest

import carousel


def fixture(match_id, kickoff, group='17:00'):
    return {'match_id': match_id, 'kickoff_utc': kickoff,
            'home_team': 'Home', 'away_team': 'Away', 'carousel_group': group}


AUGUST_16 = [fixture('1', '2026-08-16T15:00:00Z'),
             fixture('2', '2026-08-16T18:45:00Z')]
AUGUST_22 = [fixture('3', '2026-08-22T15:00:00Z'),
             fixture('4', '2026-08-22T16:30:00Z'),
             fixture('5', '2026-08-22T18:45:00Z')]


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Point carousel.py at a matches.json we control."""
    def use(entries):
        path = tmp_path / 'matches.json'
        path.write_text(json.dumps(entries), encoding='utf-8')
        monkeypatch.setattr(carousel, 'REGISTRY_FILE', str(path))
    return use


# ── The matchday in the path ──────────────────────────────────────────────────

def test_the_folder_is_scoped_by_matchday(registry):
    registry(AUGUST_22)
    assert carousel._group_root('17:00') == 'carousel/2026-08-22/17:00'


def test_the_same_name_on_two_days_is_two_folders(registry, tmp_path,
                                                  monkeypatch):
    """The actual bug: reusing '17:00' must not reuse its POSTED marker."""
    registry(AUGUST_16)
    old = carousel._posted_id('17:00')
    registry(AUGUST_22)
    new = carousel._posted_id('17:00')
    assert old != new
    assert old == 'carousel/2026-08-16/17:00/POSTED.json'
    assert new == 'carousel/2026-08-22/17:00/POSTED.json'


def test_the_day_is_the_earliest_kickoff_not_the_latest(registry):
    """A group can straddle midnight UTC; every member must still agree."""
    registry([fixture('1', '2026-08-22T22:00:00Z'),
              fixture('2', '2026-08-23T00:30:00Z')])
    assert carousel._matchday('17:00') == '2026-08-22'


def test_a_worker_and_the_dispatcher_agree(registry):
    """
    A worker knows only its own fixture, the dispatcher knows all of them.
    Both must land on the same folder or slides go somewhere nobody reads.
    """
    registry(AUGUST_22)
    late_worker = AUGUST_22[-1]          # 18:45, not the group's earliest
    assert (carousel._slide_id('17:00', '5', [late_worker])
            == carousel._slide_id('17:00', '5', AUGUST_22))


# ── When the registry can't answer ────────────────────────────────────────────

def test_it_falls_back_to_the_entries_it_was_given(registry):
    """A group already pruned out of matches.json still resolves."""
    registry([])
    assert carousel._group_root('17:00', AUGUST_22) == 'carousel/2026-08-22/17:00'


def test_an_unreadable_registry_does_not_raise(monkeypatch):
    monkeypatch.setattr(carousel, 'REGISTRY_FILE', '/nonexistent/matches.json')
    assert carousel._group_root('17:00', AUGUST_22) == 'carousel/2026-08-22/17:00'


def test_a_group_with_no_readable_kickoff_gets_one_named_bucket(registry):
    """Deterministic rather than clever: both processes still meet."""
    registry([])
    assert carousel._matchday('17:00', [{'match_id': '1'}]) == 'undated'
    assert carousel._matchday('17:00', [{'kickoff_utc': 'not a date'}]) == 'undated'


# ── The three paths stay in one folder ────────────────────────────────────────

def test_slide_manifest_and_marker_share_a_folder(registry):
    registry(AUGUST_22)
    root = carousel._group_root('17:00')
    assert carousel._slide_id('17:00', '3') == f'{root}/3'
    assert carousel._manifest_id('17:00', '3') == f'{root}/3.json'
    assert carousel._posted_id('17:00') == f'{root}/POSTED.json'
