"""
The last chance to fix an early card: the whistle.

An early card goes up at minute 45 or 90 and is corrected by the monitoring
block for the half it is in — `raw_status == '1H'` for half-time, `'2H'` for
full-time. Both stop the moment the scraper flips to HT or FT, and a goal in
stoppage time reaches the feed a poll or two *after* that flip. So the trigger
that confirms the whistle has to re-check the card before it declares the
moment done.

Dortmund vs Bayern, 2026-08-22, is the case this exists for. The early HT card
went up on 0-1 at 19:16, Olise scored on 45', the status went to HT before the
event landed, and the trigger marked half-time posted without looking. The card
sat on the feed showing the wrong score until it was deleted by hand.

`_settle_early_card` is the decision. True means the moment is finished with and
can be marked posted; False means leave it open for the next poll.
"""

import pytest

import main


ENTRY = {'match_id': '54465223',
         'home_team': 'Borussia Dortmund', 'away_team': 'Bayern Munich'}

SCRAPE = {'matchSample': {'fs_A': '0', 'fs_B': '2'}, 'events': []}

# _card_events() keys, in the shape it really emits.
BROWN = "goal|Bayern|Nathaniel Brown|28'"
OLISE = "goal|Bayern|Olise|45'"


@pytest.fixture
def settle(monkeypatch):
    """_settle_early_card with the posting stack replaced by a record of calls."""
    calls = {'deleted': [], 'posted': [], 'waited': []}
    outcome = {'repost': (['cid1'], 'ig-new')}

    main.MATCH_STATE.clear()
    main.MATCH_STATE[ENTRY['match_id']] = {
        'early_ht_ig_id': 'ig-old', 'early_ht_cloudinary_id': ['cid0'],
        'early_ft_ig_id': 'ig-old', 'early_ft_cloudinary_id': ['cid0'],
    }

    monkeypatch.setattr(main, '_save_state', lambda: None)
    monkeypatch.setattr(main, '_delete_early_post',
                        lambda entry, et: calls['deleted'].append(et))

    def fake_pipeline(entry, et, data):
        calls['posted'].append(et)
        return outcome['repost']
    monkeypatch.setattr(main, '_early_pipeline', fake_pipeline)

    def fake_wait(match_id, lagging):
        calls['waited'].append(lagging)
        return lagging          # the real one is bounded; here it just obeys
    monkeypatch.setattr(main, '_wait_for_scorers', fake_wait)

    def run(event_type='HT', stale=None, lagging=False,
            score=('0', '2'), events=(BROWN, OLISE)):
        return main._settle_early_card(ENTRY, event_type, stale, lagging,
                                       SCRAPE, score, list(events))

    run.calls = calls
    run.outcome = outcome
    return run


# ── Nothing changed ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('moment', ['HT', 'FT'])
def test_a_card_that_still_matches_is_left_alone(settle, moment):
    assert settle(moment, stale=None) is True
    assert settle.calls['deleted'] == []
    assert settle.calls['posted'] == []


def test_a_fresh_card_is_not_held_up_by_a_lagging_scorer_list(settle):
    """No correction is needed, so the scorer list is nobody's business."""
    assert settle('HT', stale=None, lagging=True) is True
    assert settle.calls['waited'] == []


# ── The Dortmund vs Bayern case ───────────────────────────────────────────────

@pytest.mark.parametrize('moment', ['HT', 'FT'])
def test_a_stale_card_is_deleted_and_reposted_at_the_whistle(settle, moment):
    assert settle(moment, stale='score 0-1→0-2') is True
    assert settle.calls['deleted'] == [moment]
    assert settle.calls['posted'] == [moment]


@pytest.mark.parametrize('moment,key', [('HT', 'ht'), ('FT', 'ft')])
def test_the_correction_becomes_the_new_baseline(settle, moment, key):
    settle(moment, stale='score 0-1→0-2', score=('0', '2'),
           events=(BROWN, OLISE))
    state = main.MATCH_STATE[ENTRY['match_id']]
    assert state[f'early_{key}_ig_id'] == 'ig-new'
    assert state[f'early_{key}_cloudinary_id'] == ['cid1']
    assert state[f'early_{key}_score'] == ['0', '2']
    assert state[f'early_{key}_events'] == [BROWN, OLISE]


# ── Waiting for the scorer to land ────────────────────────────────────────────

@pytest.mark.parametrize('moment', ['HT', 'FT'])
def test_a_lagging_scorer_list_keeps_the_moment_open(settle, moment):
    """
    The score moved but the scorer has not arrived. Marking the moment posted
    here is what loses the goal for good — the branch never runs again.
    """
    assert settle(moment, stale='score 0-1→0-2', lagging=True) is False
    assert settle.calls['deleted'] == []
    assert settle.calls['posted'] == []


def test_nothing_is_deleted_before_the_replacement_can_be_drawn(settle):
    """Deleting first and failing to repost would leave the feed with nothing."""
    settle('HT', stale=f'new event: {OLISE}', lagging=True)
    assert settle.calls['deleted'] == []


# ── When the repost fails ─────────────────────────────────────────────────────

@pytest.mark.parametrize('failure', [(None, None), ([], None), (['cid'], None)])
@pytest.mark.parametrize('moment', ['HT', 'FT'])
def test_a_failed_repost_leaves_the_moment_open(settle, moment, failure):
    """
    _delete_early_post has already cleared early_<moment>_ig_id, so the next
    poll misses the "early post active" branch and runs the fallback pipeline.
    Settling here would end the match with no card at all.
    """
    settle.outcome['repost'] = failure
    assert settle(moment, stale='score 0-1→0-2') is False
    assert settle.calls['deleted'] == [moment]


def test_a_failed_repost_does_not_overwrite_the_baseline(settle):
    settle.outcome['repost'] = (None, None)
    settle('HT', stale='score 0-1→0-2', score=('0', '2'))
    assert 'early_ht_score' not in main.MATCH_STATE[ENTRY['match_id']]
