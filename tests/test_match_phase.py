"""
derive_status() and the signals it leans on.

Every posting gate in the worker reads the phase this returns, so a wrong
answer here is a wrong card — or no card at all. The awkward cases are the
ones the scraper's clock cannot express on its own: it sits on 45 through
first-half stoppage time *and* the interval, and stops at 120 once a shootout
starts.
"""

import pytest

from helpers import goal, scrape
from main import _at_half_time, _get_minute, _has_penalties, derive_status


class TestMinute:
    def test_plain_minute(self):
        assert _get_minute(scrape(minute='67')) == 67

    def test_stoppage_time_truncates_to_the_base_minute(self):
        assert _get_minute(scrape(minute='90+3')) == 90

    @pytest.mark.parametrize('raw', ['', 'HT', None])
    def test_unparsable_minute_is_zero_rather_than_a_crash(self, raw):
        assert _get_minute(scrape(minute=raw)) == 0


class TestPhase:
    def test_not_started(self):
        assert derive_status(scrape(status='Fixture')) == 'NS'

    def test_played_is_full_time_whatever_the_clock_says(self):
        # 'Played' is the scraper's own end-of-match flag and always wins.
        assert derive_status(scrape(status='Played', minute='45')) == 'FT'

    @pytest.mark.parametrize('minute,expected', [
        ('1', '1H'), ('44', '1H'), ('46', '2H'), ('90', '2H'),
        ('91', 'ET'), ('120', 'ET'),
    ])
    def test_minute_picks_the_half(self, minute, expected):
        assert derive_status(scrape(minute=minute)) == expected

    def test_minute_45_without_an_interval_signal_is_still_the_first_half(self):
        # First-half stoppage time: the clock reads 45 but the whistle has not
        # gone. Calling this HT would post the half-time card early.
        assert derive_status(scrape(minute='45')) == '1H'

    def test_minute_45_with_a_half_time_event_is_the_interval(self):
        data = scrape(minute='45', events=[goal(type='half_time')])
        assert derive_status(data) == 'HT'

    def test_minute_45_with_a_half_time_score_is_the_interval(self):
        # The half-time scoreline is only filled in at the whistle, so it is
        # sufficient on its own.
        assert derive_status(scrape(minute='45', hts=('1', '0'))) == 'HT'

    def test_a_shootout_overrides_the_stopped_clock(self):
        # The clock stops at 120 once penalties begin, so the minute alone
        # would report ET forever.
        data = scrape(minute='120', fs=('1', '1'), ps=('4', '3'))
        assert derive_status(data) == 'AP'


class TestHalfTimeSignals:
    def test_no_signal(self):
        assert _at_half_time(scrape(minute='45')) is False

    def test_blank_half_time_score_does_not_count(self):
        assert _at_half_time(scrape(minute='45', hts=('', ''))) is False

    def test_malformed_events_do_not_crash_the_check(self):
        data = scrape(minute='45')
        data['events'] = 'not a list'
        assert _at_half_time(data) is False


class TestPenaltyDetection:
    def test_both_sides_needed(self):
        assert _has_penalties({'ps_A': '4', 'ps_B': '3'}) is True
        assert _has_penalties({'ps_A': '4', 'ps_B': ''}) is False
        assert _has_penalties({}) is False
