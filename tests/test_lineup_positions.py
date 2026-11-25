"""
The starting XI only posts once every starter has a position.

The feed publishes names first and grid positions a few minutes later — and
for some matches never (Newcastle vs Strasbourg, three Napoli friendlies in the
archive). Names alone used to go out near kickoff as a listed team sheet,
which read as a straight line on the feed. Now the worker waits for the grid,
and skips with a 🙋 alert at the deadline if it never arrives.
"""

from datetime import datetime, timedelta, timezone

import lineup_card
import main
import matchlog


KICKOFF = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
ENTRY = {'match_id': '99000002', 'home_team': 'Home', 'away_team': 'Away',
         'kickoff_utc': '2026-11-25T20:00:00Z', 'post_lineups': True,
         'scraper_url': 'http://x'}

BANDS = ['GK', 'D1', 'D1', 'D1', 'D1', 'M', 'M', 'M', 'A', 'A', 'A']
SIDES = ['C', 'L', 'CL', 'CR', 'R', 'CL', 'C', 'CR', 'L', 'C', 'R']


def _xi(placed=11):
    """Eleven starters, the first `placed` of them on the grid."""
    xi = []
    for i in range(11):
        p = {'person_id': i, 'person': f'Player {i}', 'shirtnumber': str(i + 1)}
        if i < placed:
            p.update(position_x=BANDS[i], position_y=SIDES[i])
        xi.append(p)
    return xi


def _data(home=11, away=11):
    return {'matchSample': {'team_A_name': 'Home', 'team_B_name': 'Away'},
            'matchFormation': {'team_A': {'lineups': _xi(home), 'sub': []},
                               'team_B': {'lineups': _xi(away), 'sub': []}}}


# ── What counts as positioned ────────────────────────────────────────────────

def test_a_fully_placed_xi_has_positions():
    assert lineup_card.has_positions(_data())


def test_names_alone_do_not():
    assert lineup_card.has_lineups(_data(0, 0))
    assert not lineup_card.has_positions(_data(0, 0))


def test_a_half_filled_grid_does_not():
    # build_rows would put the unplaced players into one straight row.
    assert not lineup_card.has_positions(_data(home=6))
    assert not lineup_card.has_positions(_data(away=10))
    assert lineup_card.has_positions(_data(away=10), side='home')


# ── When the worker decides to post ──────────────────────────────────────────

def test_names_without_positions_never_post_even_at_kickoff():
    for minutes in (-30, -5, -1, 0, 4):
        now = KICKOFF + timedelta(minutes=minutes)
        assert not main._lineup_ready(ENTRY, _data(0, 0), now), minutes
        assert not main._lineup_ready(ENTRY, _data(home=8), now), minutes


def test_it_posts_as_soon_as_the_grid_is_complete():
    now = KICKOFF - timedelta(minutes=50)
    assert main._lineup_ready(ENTRY, _data(), now)


def test_the_card_refuses_to_draw_without_positions():
    assert lineup_card.generate_lineup_card(_data(0, 0), side='home') is None
    assert lineup_card.generate_lineup_card(_data(home=5), side='home') is None


# ── The deadline ─────────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


def _run_to_deadline(monkeypatch, data):
    """Drive the worker one poll past the line-up deadline; return alerts."""
    alerts, posted = [], []
    for name in ('start', 'log', 'warn', 'error', 'finish'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)
    for name, fn in {
        '_save_state': lambda: None,
        'get_crest_url': lambda *a, **k: 'crest',
        'upsert_match': lambda e: None,
        'enrich_with_desktop': lambda *a, **k: None,
        '_run_event_photos': lambda *a, **k: None,
        '_clarify_staged_photos': lambda *a, **k: None,
        '_photo_reminder': lambda *a, **k: None,
        '_clear_event_photos': lambda *a, **k: None,
        'is_event_posted': lambda *a: False,
        'send_alert': lambda text, **k: alerts.append(text),
        '_run_lineup_pipeline': lambda *a: posted.append(1) or True,
    }.items():
        monkeypatch.setattr(main, name, fn)

    polls = iter([dict(data, status='Fixture')])

    def fake_get(url):
        try:
            return next(polls)
        except StopIteration:
            raise _Stop

    monkeypatch.setattr(main, 'get_match_data', fake_get)
    monkeypatch.setattr(main.time, 'sleep', lambda s: None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return KICKOFF + timedelta(minutes=6)

    monkeypatch.setattr(main, 'datetime', Clock)
    main.MATCH_STATE.clear()
    try:
        main.match_worker(dict(ENTRY))
    except _Stop:
        pass
    return alerts, posted


def test_deadline_alert_says_the_positions_never_came(monkeypatch):
    alerts, posted = _run_to_deadline(monkeypatch, _data(0, 0))
    assert not posted
    (alert,) = [a for a in alerts if a.startswith('🙋')]
    assert 'never with player positions' in alert


def test_deadline_alert_when_nothing_was_published(monkeypatch):
    data = _data()
    data['matchFormation'] = {}
    alerts, posted = _run_to_deadline(monkeypatch, data)
    assert not posted
    (alert,) = [a for a in alerts if a.startswith('🙋')]
    assert 'no starting XI was published in time' in alert
