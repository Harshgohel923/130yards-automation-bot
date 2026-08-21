"""
Hand-entered matches parse the way a person types them.

Everything here runs on typed input, which is the one place in this codebase
where the data has not already been through a scraper. A misread date or a
dropped scorer becomes a public post with the wrong facts on it, and nothing
downstream can tell — the dict manual_match produces is indistinguishable from
a scraped one by design.

The parsers are also the bot's error messages: each ParseError is shown to the
user verbatim, so the tests check that a bad field is rejected rather than
guessed at.
"""

from datetime import datetime, timezone

import pytest
from helpers import scrape

import manual_match as mm


# ── Scores ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('2-1', ('2', '1')),
    ('0-0', ('0', '0')),
    (' 3 - 2 ', ('3', '2')),
    ('2:1', ('2', '1')),
    ('2 – 1', ('2', '1')),      # en dash, which is what a phone keyboard gives
    ('10-0', ('10', '0')),
])
def test_score_forms(text, expected):
    assert mm.parse_score(text) == expected


@pytest.mark.parametrize('text', ['2', 'two-one', '2-', '', '2-1-1', 'x'])
def test_unreadable_scores_are_rejected(text):
    with pytest.raises(mm.ParseError):
        mm.parse_score(text)


# ── Dates ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    '21/08/2026', '2026-08-21', '21-08-2026', '21.08.2026',
    '21 Aug 2026', '21 August 2026', '21/08/26',
])
def test_date_forms_all_mean_the_same_day(text):
    assert mm.parse_date(text) == datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_ambiguous_date_is_read_day_first():
    """03/08 is 3 August, not 8 March — this is a European football page."""
    assert mm.parse_date('03/08/2026').month == 8


@pytest.mark.parametrize('text', ['yesterday', '32/01/2026', '', 'last week'])
def test_unreadable_dates_are_rejected(text):
    with pytest.raises(mm.ParseError):
        mm.parse_date(text)


def test_card_date_is_upper_case_and_zero_padded():
    assert mm.format_card_date(mm.parse_date('2026-08-03')) == '03 AUG 2026'


# ── Scorer lines ──────────────────────────────────────────────────────────────

def test_plain_line_is_a_goal():
    [ev] = mm.parse_scorers('23 Saka', 'Arsenal')
    assert ev == {'type': 'goal', 'team': 'Arsenal', 'player': 'Saka',
                  'minute': "23'", 'minute_extra': ''}


def test_stoppage_time_is_kept_as_the_scraper_stores_it():
    """The renderers rebuild '45+2' from these two fields, so the offset must
    not be folded into the minute here."""
    [ev] = mm.parse_scorers('45+2 Havertz (pen)', 'Arsenal')
    assert ev['minute'] == "45'"
    assert ev['minute_extra'] == '2'
    assert ev['type'] == 'penalty_goal'


@pytest.mark.parametrize('suffix, expected', [
    ('(pen)', 'penalty_goal'),
    ('(PEN)', 'penalty_goal'),
    ('(penalty)', 'penalty_goal'),
    ('(og)', 'own_goal'),
    ('(own goal)', 'own_goal'),
    ('(red)', 'red_card'),
    ('(rc)', 'red_card'),
    ('(miss)', 'penalty_missed'),
    ('(missed penalty)', 'penalty_missed'),
])
def test_event_type_suffixes(suffix, expected):
    [ev] = mm.parse_scorers(f'23 Someone {suffix}', 'Arsenal')
    assert ev['type'] == expected


@pytest.mark.parametrize('line', [
    "23' Saka",         # the apostrophe people type
    '23. Saka',
    '23 - Saka',
    '  23   Saka  ',
])
def test_separator_variations(line):
    [ev] = mm.parse_scorers(line, 'Arsenal')
    assert (ev['player'], ev['minute']) == ('Saka', "23'")


def test_multi_word_names_survive():
    [ev] = mm.parse_scorers('67 Bruno Guimarães (pen)', 'Newcastle')
    assert ev['player'] == 'Bruno Guimarães'


@pytest.mark.parametrize('text', ['none', 'None', '-', 'n/a', '', '   '])
def test_ways_of_saying_there_were_none(text):
    assert mm.parse_scorers(text, 'Arsenal') == []


def test_blank_lines_between_scorers_are_ignored():
    assert len(mm.parse_scorers('23 Saka\n\n67 Rice\n', 'Arsenal')) == 2


def test_one_bad_line_rejects_the_whole_block():
    """A half-read list is worse than a rejected one: the card would look
    complete with a goal quietly missing from it."""
    with pytest.raises(mm.ParseError) as e:
        mm.parse_scorers('23 Saka\nsomebody scored\n67 Rice', 'Arsenal')
    assert 'somebody scored' in str(e.value)


def test_unknown_event_type_names_itself_in_the_error():
    with pytest.raises(mm.ParseError) as e:
        mm.parse_scorers('23 Saka (assist)', 'Arsenal')
    assert 'assist' in str(e.value)


# ── Assembly ──────────────────────────────────────────────────────────────────

@pytest.fixture
def built():
    when = mm.parse_date('21/08/2026')
    return mm.build_scraper_data(
        home_team='Arsenal', away_team='Man City',
        home_score='3', away_score='1',
        competition='Premier League', when=when, event_type='FT',
        home_events=mm.parse_scorers('23 Saka\n45+2 Havertz (pen)\n88 Odegaard',
                                     'Arsenal'),
        away_events=mm.parse_scorers('67 Rice (og)\n90 Foden (miss)', 'Man City'),
        venue='Emirates Stadium',
    )


def test_shape_matches_what_the_renderers_read(built):
    assert set(built) >= {'matchSample', 'events', 'matchFormation',
                          'statistics', 'matchAnalysis'}
    sample = built['matchSample']
    assert sample['team_A_name'] == 'Arsenal'
    assert sample['team_B_name'] == 'Man City'
    assert built['matchFormation']['venue_name'] == 'Emirates Stadium'


def test_full_time_fills_fs_not_hts(built):
    """scorecard.py reads fs_* at full time and hts_* at half time. A guessed
    half-time score would be drawn as fact, so it is left absent."""
    assert (built['matchSample']['fs_A'], built['matchSample']['fs_B']) == ('3', '1')
    assert 'hts_A' not in built['matchSample']


def test_half_time_fills_hts_not_fs():
    data = mm.build_scraper_data(
        home_team='A', away_team='B', home_score='1', away_score='0',
        competition='Friendly', when=mm.parse_date('2026-08-21'),
        event_type='HT', home_events=[], away_events=[])
    assert (data['matchSample']['hts_A'], data['matchSample']['hts_B']) == ('1', '0')
    assert 'fs_A' not in data['matchSample']


def test_events_are_ordered_by_minute_then_stoppage(built):
    minutes = [(e['minute'], e['minute_extra']) for e in built['events']]
    assert minutes == [("23'", ''), ("45'", '2'), ("67'", ''),
                       ("88'", ''), ("90'", '')]


def test_events_carry_the_team_they_were_entered_under(built):
    by_team = {e['player']: e['team'] for e in built['events']}
    assert by_team['Saka'] == 'Arsenal'
    assert by_team['Rice'] == 'Man City'


def test_card_date_is_set(built):
    assert built['matchSample']['card_date'] == '21 AUG 2026'


def test_only_manual_matches_carry_a_card_date():
    """The renderers draw the date if and only if this field is present, which
    is how scraped fixtures are kept free of one."""
    assert 'card_date' not in scrape()['matchSample']


def test_goal_count_ignores_cards_and_misses(built):
    assert mm.goal_count(built['events'], 'Arsenal') == 3
    assert mm.goal_count(built['events'], 'Man City') == 1


def test_match_id_is_not_mistakable_for_a_scraped_one(built):
    match_id = built['matchSample']['match_id']
    assert match_id.startswith('manual-')
    assert not match_id.isdigit()


def test_match_id_is_stable_for_the_same_match():
    """Template choice is seeded by match_id, so re-entering a match has to
    land on the same design rather than a new random one."""
    when = mm.parse_date('2026-08-21')
    assert (mm.new_match_id(when, 'Arsenal', 'Man City')
            == mm.new_match_id(when, 'Arsenal', 'Man City'))


def test_different_matches_get_different_ids():
    when = mm.parse_date('2026-08-21')
    assert (mm.new_match_id(when, 'Arsenal', 'Man City')
            != mm.new_match_id(when, 'Chelsea', 'Spurs'))


def test_non_latin_team_names_still_produce_an_id():
    """re.sub strips everything non-A-Za-z, so a fully non-Latin name would
    otherwise leave an empty string where the initials go."""
    assert mm.new_match_id(mm.parse_date('2026-08-21'), 'Зенит', '上海海港')


# ── Rejecting what doesn't fit the format ─────────────────────────────────────
# The team and competition steps are the only ones with no grammar of their
# own. Everything else is bounded by its own shape; these two would otherwise
# accept literally anything, including an answer meant for a different step.

class TestTeamNames:
    def test_an_ordinary_name_passes(self):
        assert mm.parse_team('  Real   Betis ') == 'Real Betis'

    def test_newlines_collapse(self):
        assert mm.parse_team('Man\nCity') == 'Man City'

    def test_non_latin_names_pass(self):
        assert mm.parse_team('Зенит') == 'Зенит'

    @pytest.mark.parametrize('text', ['', '   ', '\n'])
    def test_empty_is_rejected(self, text):
        with pytest.raises(mm.ParseError, match='empty'):
            mm.parse_team(text)

    @pytest.mark.parametrize('text', ['1234', '!!!', '@#$%'])
    def test_something_with_no_letters_is_rejected(self, text):
        with pytest.raises(mm.ParseError, match='no letters'):
            mm.parse_team(text)

    def test_too_long_says_how_long(self):
        with pytest.raises(mm.ParseError) as e:
            mm.parse_team('A' * (mm.MAX_TEAM_NAME + 1))
        assert str(mm.MAX_TEAM_NAME) in str(e.value)
        assert str(mm.MAX_TEAM_NAME + 1) in str(e.value)

    def test_exactly_at_the_limit_passes(self):
        assert mm.parse_team('A' * mm.MAX_TEAM_NAME)

    @pytest.mark.parametrize('text, looks_like', [
        ('2-1', 'a score'),
        ('0-0', 'a score'),
        ('21/08/2026', 'a date'),
        ('21 Aug 2026', 'a date'),
    ])
    def test_an_answer_from_another_step_is_named_as_such(self, text, looks_like):
        """Ten questions in a row is exactly the shape of interaction where an
        answer lands one step early. A scoreline accepted here would become a
        team called '2-1', render, and post."""
        with pytest.raises(mm.ParseError) as e:
            mm.parse_team(text)
        assert looks_like in str(e.value)

    def test_a_real_name_that_contains_digits_still_passes(self):
        assert mm.parse_team('FC Schalke 04') == 'FC Schalke 04'


class TestCompetition:
    def test_an_ordinary_name_passes(self):
        assert mm.parse_competition('Premier League') == 'Premier League'

    def test_it_has_more_room_than_a_team_name(self):
        assert mm.MAX_COMPETITION > mm.MAX_TEAM_NAME
        assert mm.parse_competition('A' * mm.MAX_TEAM_NAME + 'BBB')

    def test_a_date_typed_here_is_rejected(self):
        with pytest.raises(mm.ParseError, match='a date'):
            mm.parse_competition('21/08/2026')


class TestDateRange:
    """The grammar can't see a transposed year — 21/08/2062 parses perfectly."""

    NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

    def test_today_passes(self):
        assert mm.parse_date('21/08/2026', now=self.NOW)

    def test_the_distant_past_passes(self):
        assert mm.parse_date('30/07/1966', now=self.NOW).year == 1966

    def test_a_future_year_is_rejected(self):
        with pytest.raises(mm.ParseError, match='future'):
            mm.parse_date('21/08/2062', now=self.NOW)

    def test_tomorrow_is_allowed_because_timezones(self):
        assert mm.parse_date('22/08/2026', now=self.NOW)

    def test_before_football_is_rejected(self):
        with pytest.raises(mm.ParseError, match='typo'):
            mm.parse_date('21/08/1850', now=self.NOW)

    def test_the_error_shows_the_shapes_it_wants(self):
        with pytest.raises(mm.ParseError) as e:
            mm.parse_date('sometime last week', now=self.NOW)
        assert '21/08/2026' in str(e.value)
        assert '2026-08-21' in str(e.value)


class TestScorerBounds:
    """The line grammar takes any 1–3 digit minute, which is how '900 Saka'
    gets through it."""

    def test_an_impossible_minute_is_rejected(self):
        with pytest.raises(mm.ParseError, match='900'):
            mm.parse_scorers('900 Saka', 'Arsenal')

    def test_minute_zero_is_rejected(self):
        with pytest.raises(mm.ParseError):
            mm.parse_scorers('0 Saka', 'Arsenal')

    def test_the_last_plausible_minute_passes(self):
        assert mm.parse_scorers(f'{mm.MAX_MINUTE} Saka', 'Arsenal')

    def test_absurd_stoppage_time_is_rejected(self):
        with pytest.raises(mm.ParseError, match='stoppage'):
            mm.parse_scorers(f'90+{mm.MAX_STOPPAGE + 1} Saka', 'Arsenal')

    def test_a_name_with_no_letters_is_rejected(self):
        with pytest.raises(mm.ParseError, match="isn't a name"):
            mm.parse_scorers('23 123', 'Arsenal')

    def test_an_overlong_name_is_rejected(self):
        with pytest.raises(mm.ParseError, match='too long'):
            mm.parse_scorers('23 ' + 'A' * (mm.MAX_PLAYER_NAME + 1), 'Arsenal')

    def test_the_bad_line_is_quoted_back_verbatim(self):
        """The message is shown in the chat as-is, so it has to name the line
        that was wrong rather than the position of it."""
        with pytest.raises(mm.ParseError) as e:
            mm.parse_scorers('23 Saka\n900 Rice\n67 Odegaard', 'Arsenal')
        listed = [ln for ln in str(e.value).splitlines() if ln.startswith('•')]
        assert any('900 Rice' in ln for ln in listed)
        assert not any('Saka' in ln or 'Odegaard' in ln for ln in listed)
