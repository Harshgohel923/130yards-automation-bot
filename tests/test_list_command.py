"""
/list: the registry as a person reads it.

The fixture registry is edited as JSON in the repo, so the question this
command answers — "what is armed for today, and what will actually post?" —
is otherwise only answerable by reading matches.json. That makes the listing
worth pinning in one specific way: it must agree with the dispatcher. Every
flag here has a default in main.py (half time on unless switched off, lineups
off unless switched on) and one has a condition (a stats slide is dropped for
a grouped match), and a listing that quietly disagreed with any of them would
send someone to check the wrong fixture.

The rest is what the times and the carousel section are for: a kick-off shown
in both zones and derived from kickoff_utc rather than trusted from the file,
and the groups spelled out, since a typo in one carousel_group splits a group
in two and nothing on the individual fixture shows it.
"""

import asyncio
import types

import pytest

import telegram_bot as tb


def fixture(**over):
    base = {
        'match_id': '111',
        'home_team': 'Argentina',
        'away_team': 'Brazil',
        'competition': 'Friendly',
        'kickoff_utc': '2026-08-23T13:00:00Z',
    }
    base.update(over)
    return base


class Msg:
    """A Telegram message that records what was said back to it."""

    def __init__(self, sent):
        self.sent = sent

    async def reply_text(self, text, **kw):
        self.sent.append({'text': text, **kw})
        return Msg(self.sent)


def update(sent, user_id=7):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id),
        message=Msg(sent),
        callback_query=None,
    )


def ctx():
    return types.SimpleNamespace(chat_data={'menu_shown': True}, user_data={})


@pytest.fixture
def registry(monkeypatch):
    """Install a fixture list; return the messages /list sends for it."""
    def run(matches):
        monkeypatch.setattr(tb, '_load_matches', lambda: list(matches))
        monkeypatch.setattr(tb, '_allowed', lambda update: True)
        sent = []
        asyncio.run(tb.list_cmd(update(sent), ctx()))
        return [m['text'] for m in sent]
    return run


class TestKickoffTimes:
    def test_both_zones_come_off_the_same_utc_instant(self):
        local, ist = tb._kickoff_times(fixture(kickoff_utc='2026-08-22T23:30:00Z'))
        assert local == 'Sun 23 Aug 2026, 01:30 CEST'
        assert ist == 'Sun 23 Aug 2026, 05:00 IST'

    def test_winter_gets_cet_not_cest(self):
        local, _ = tb._kickoff_times(fixture(kickoff_utc='2026-12-26T15:00:00Z'))
        assert local.endswith('16:00 CET')

    def test_a_stale_stored_time_is_ignored_in_favour_of_utc(self):
        local, ist = tb._kickoff_times(fixture(kickoff_local='wrong',
                                               kickoff_ist='also wrong'))
        assert 'wrong' not in local and 'wrong' not in ist

    def test_only_an_unparseable_utc_falls_back_to_the_file(self):
        local, ist = tb._kickoff_times(fixture(kickoff_utc='soon',
                                               kickoff_local='Sat, 20:00 CEST',
                                               kickoff_ist='Sat, 23:30 IST'))
        assert (local, ist) == ('Sat, 20:00 CEST', 'Sat, 23:30 IST')

    def test_no_time_at_all_is_listed_rather_than_crashing(self):
        assert tb._kickoff_times({}) == ('?', '?')


class TestPostingPlan:
    """Each default is the dispatcher's, not this listing's."""

    def test_half_time_is_on_when_the_flag_is_absent(self):
        assert 'Half time: on' in tb._posting_plan(fixture())

    def test_half_time_off_is_reported_off(self):
        assert 'Half time: off' in tb._posting_plan(fixture(post_ht=False))

    def test_lineups_are_off_when_the_flag_is_absent(self):
        assert 'Lineups: off' in tb._posting_plan(fixture())

    def test_lineups_lead_with_the_home_side_by_default(self):
        assert 'Lineups: on (home first)' in tb._posting_plan(
            fixture(post_lineups=True))

    def test_lineups_first_away_is_shown(self):
        assert 'Lineups: on (away first)' in tb._posting_plan(
            fixture(post_lineups=True, lineups_first='away'))

    def test_an_unrecognised_lineups_first_reads_as_home(self):
        assert 'Lineups: on (home first)' in tb._posting_plan(
            fixture(post_lineups=True, lineups_first='HOEM'))

    def test_a_stats_slide_is_reported_off_for_a_grouped_match(self):
        plan = tb._posting_plan(fixture(post_ft_stats=True,
                                        carousel_group='sunday'))
        assert any(line.startswith('FT stats: off') for line in plan)

    def test_a_stats_slide_is_on_for_a_match_that_posts_alone(self):
        assert 'FT stats: on' in tb._posting_plan(fixture(post_ft_stats=True))

    def test_knockout_is_mentioned_only_when_set(self):
        assert not any('Knockout' in l for l in tb._posting_plan(fixture()))
        assert any('Knockout' in l
                   for l in tb._posting_plan(fixture(knockout_match=True)))


class TestCarouselSummary:
    def test_says_so_when_nothing_is_grouped(self):
        assert 'No carousel groups' in tb._carousel_summary([fixture()])

    def test_names_every_match_in_a_group(self):
        text = tb._carousel_summary([
            fixture(match_id='1', carousel_group='sunday'),
            fixture(match_id='2', home_team='Hull City', away_team='Man Utd',
                    carousel_group='sunday'),
        ])
        assert 'sunday — 2 matches' in text
        assert 'Argentina vs Brazil' in text and 'Hull City vs Man Utd' in text

    def test_a_group_of_one_is_flagged_as_posting_alone(self):
        # The shape a typo in one carousel_group leaves behind.
        text = tb._carousel_summary([
            fixture(match_id='1', carousel_group='sunday'),
            fixture(match_id='2', carousel_group='sundy'),
        ])
        assert text.count('posts alone') == 2

    def test_a_blank_group_is_not_a_group(self):
        assert 'No carousel groups' in tb._carousel_summary(
            [fixture(carousel_group='   ')])


class TestTheReply:
    def test_an_empty_registry_says_so(self, registry):
        assert 'No matches found' in registry([])[0]

    def test_fixtures_are_listed_earliest_first(self, registry):
        text = '\n'.join(registry([
            fixture(match_id='2', home_team='Late', away_team='Kickoff',
                    kickoff_utc='2026-08-23T19:00:00Z'),
            fixture(match_id='1', home_team='Early', away_team='Kickoff'),
        ]))
        assert text.index('Early') < text.index('Late')

    def test_an_unparseable_kickoff_sorts_last_rather_than_crashing(self, registry):
        text = '\n'.join(registry([
            fixture(match_id='1', home_team='Broken', kickoff_utc='soon'),
            fixture(match_id='2', home_team='Fine'),
        ]))
        assert text.index('Fine') < text.index('Broken')

    def test_no_utc_time_is_shown_anywhere(self, registry):
        text = '\n'.join(registry([fixture()]))
        assert 'UTC' not in text and '2026-08-23T13:00:00Z' not in text

    def test_both_readings_of_the_kickoff_are_in_the_listing(self, registry):
        text = '\n'.join(registry([fixture()]))
        assert '15:00 CEST' in text and '18:30 IST' in text

    def test_the_carousel_section_comes_with_the_listing(self, registry):
        text = '\n'.join(registry([fixture(carousel_group='sunday')]))
        assert 'Carousel groups' in text

    def test_a_long_registry_is_split_without_breaking_a_fixture(self, registry):
        many = [fixture(match_id=str(i), home_team=f'Home {i}')
                for i in range(60)]
        messages = registry(many)
        assert len(messages) > 1
        assert all(len(m) <= tb.MAX_MESSAGE for m in messages)
        # Every fixture is listed whole, in exactly one of the messages.
        for i in range(60):
            assert sum(f'Home {i} vs Brazil' in m for m in messages) == 1

    def test_an_unauthorized_user_is_told_so_and_shown_nothing(self, monkeypatch):
        monkeypatch.setattr(tb, '_allowed', lambda update: False)
        monkeypatch.setattr(tb, '_load_matches', lambda: [fixture()])
        sent = []
        asyncio.run(tb.list_cmd(update(sent), ctx()))
        assert len(sent) == 1 and 'Not authorized' in sent[0]['text']


class TestTheCommandIsWiredUp:
    def test_it_is_offered_in_the_menu(self):
        assert 'list' in tb.KNOWN_COMMANDS

    def test_help_explains_it(self):
        assert '/list' in tb.HELP_TEXT
