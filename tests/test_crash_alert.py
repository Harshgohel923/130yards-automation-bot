"""
What a crash alert says was already posted.

A restarted worker cannot work this out for itself — state.json and bot.db are
both local to the run — so the alert has to carry it while the knowledge still
exists. Getting it wrong sends someone to re-run a match that already posted.
"""

import pytest

import main


@pytest.fixture
def state():
    """Give a test its own entry in the worker's global state, then clear it."""
    match_id = 'test-match'
    def install(**flags):
        with main.STATE_LOCK:
            main.MATCH_STATE[match_id] = dict(flags)
        return match_id
    yield install
    with main.STATE_LOCK:
        main.MATCH_STATE.pop(match_id, None)


def test_nothing_posted(state):
    assert main._posted_so_far(state()) == 'Nothing had been posted for it yet.'


def test_a_match_the_worker_never_reached(state):
    # Crashed before the state entry existed at all.
    assert 'Nothing had been posted' in main._posted_so_far('never-seen')


def test_lineups_only(state):
    assert main._posted_so_far(state(lineups_posted=True)) == \
        'Already posted: the line-ups.'


def test_early_and_confirmed_half_time_are_one_card(state):
    # Both flags set means a single card is live — the early one, since the
    # HT trigger confirms it rather than posting again.
    assert main._posted_so_far(state(early_ht_posted=True, ht_posted=True)) == \
        'Already posted: a half-time card.'


def test_fallback_half_time_only(state):
    assert main._posted_so_far(state(ht_posted=True)) == \
        'Already posted: the half-time card.'


def test_full_sequence_lists_each_card_once(state):
    line = main._posted_so_far(state(
        lineups_posted=True, early_ht_posted=True, ht_posted=True,
        early_ft_posted=True, ft_posted=True))
    assert line.count('half-time') == 1
    assert line.count('full-time') == 1
    assert 'line-ups' in line


def test_false_flags_are_not_reported(state):
    assert main._posted_so_far(state(ht_posted=False, ft_posted=False)) == \
        'Nothing had been posted for it yet.'
