"""
Event classification from the scraper's icon URLs.

allfootball encodes the event type in an opaque substring of the icon
filename, so this table is the only thing separating a goal from a
substitution. It has no way to notice a filename it has never seen — an
unrecognised icon becomes 'other' and the event silently drops off the card —
so at minimum the mapping we do know has to stay pinned down.
"""

import pytest

from football_scraper_dom import parse_event_type


BASE = 'https://img.allfootballapp.com/www/M00/00/00/'


@pytest.mark.parametrize('token,expected', [
    ('cp-_', 'goal'),
    ('oamr', 'own_goal'),
    ('tqm6', 'penalty_goal'),
    ('kbao', 'penalty_goal'),
    ('emgp', 'penalty_missed'),
    ('qwrv', 'assist'),
    ('l8te', 'yellow_card'),
    ('widj', 'red_card'),
    ('ualv', 'substitution_in'),
    ('ekur', 'substitution_out'),
])
def test_known_icons_classify(token, expected):
    event_type, _symbol = parse_event_type(f'{BASE}{token}xxxx.png')
    assert event_type == expected


def test_every_known_type_has_a_symbol():
    for token in ('cp-_', 'oamr', 'tqm6', 'emgp', 'qwrv', 'l8te', 'widj'):
        _type, symbol = parse_event_type(f'{BASE}{token}xxxx.png')
        assert symbol and symbol != '❓'


class TestSpecialCases:
    def test_half_time_is_matched_on_the_exact_filename(self):
        assert parse_event_type(f'{BASE}ht.png')[0] == 'half_time'

    def test_var_is_matched_within_the_filename(self):
        assert parse_event_type(f'{BASE}var-review.png')[0] == 'var'

    def test_missing_url_is_unknown(self):
        assert parse_event_type('')[0] == 'unknown'
        assert parse_event_type(None)[0] == 'unknown'

    def test_unrecognised_icon_falls_through_to_other(self):
        # The silent-drift case: a rotated filename lands here rather than
        # raising, and the event vanishes from the card.
        assert parse_event_type(f'{BASE}zzzz9999.png')[0] == 'other'


class TestOrdering:
    def test_substitutions_are_checked_before_the_goal_family(self):
        # 'ualv' and a goal token in one URL must resolve as the substitution;
        # the ordering in parse_event_type is load-bearing.
        assert parse_event_type(f'{BASE}ualv-cp-_.png')[0] == 'substitution_in'

    def test_case_is_ignored(self):
        assert parse_event_type(f'{BASE}L8TE0000.PNG')[0] == 'yellow_card'
