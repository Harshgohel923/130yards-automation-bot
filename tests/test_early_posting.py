"""
The guards around early cards.

An early card goes up before the whistle, so it can be wrong by the time the
whistle comes. Two decisions keep that under control:

  _events_match_score  — is the scorer list caught up enough to draw a card?
  _early_card_stale    — does the card that is already live still match?

Getting the second one wrong in either direction is visible on the feed: too
eager and a correct card is deleted and reposted, too shy and a wrong scoreline
stays up.
"""

from helpers import goal, scrape
from main import _card_events, _early_card_stale, _events_match_score


class TestEventsMatchScore:
    def test_goalless_match_is_accounted_for(self):
        assert _events_match_score(scrape()) is True

    def test_scorer_present_for_the_only_goal(self):
        data = scrape(fs=('1', '0'), events=[goal()])
        assert _events_match_score(data) is True

    def test_scoreboard_ahead_of_the_scorer_list(self):
        # The classic injury-time case: fs_A moved, the event has not landed.
        assert _events_match_score(scrape(fs=('1', '0'))) is False

    def test_own_goals_and_penalties_count_as_goals(self):
        data = scrape(fs=('2', '0'),
                      events=[goal(type='own_goal'), goal(type='penalty_goal')])
        assert _events_match_score(data) is True

    def test_non_goal_events_do_not_count(self):
        data = scrape(fs=('1', '0'), events=[goal(type='yellow_card')])
        assert _events_match_score(data) is False

    def test_totals_compare_across_both_teams(self):
        # Own goals make per-team attribution ambiguous, so only totals matter.
        data = scrape(fs=('1', '1'), events=[goal(team='A'), goal(team='A')])
        assert _events_match_score(data) is True

    def test_unparsable_score_does_not_block_posting(self):
        assert _events_match_score(scrape(fs=('?', '0'))) is True

    def test_malformed_events_block_only_when_goals_exist(self):
        goalless = scrape()
        goalless['events'] = None
        assert _events_match_score(goalless) is True

        scored = scrape(fs=('1', '0'))
        scored['events'] = None
        assert _events_match_score(scored) is False


class TestCardEvents:
    def test_only_displayed_event_types_are_included(self):
        data = scrape(events=[
            goal(player='Scorer'),
            goal(type='red_card', player='Sent Off'),
            goal(type='penalty_missed', player='Missed'),
            goal(type='yellow_card', player='Booked'),
            goal(type='substitution_in', player='Sub'),
        ])
        keys = _card_events(data)
        assert len(keys) == 3
        assert not any('Booked' in k or 'Sub' in k for k in keys)

    def test_output_is_sorted_so_two_polls_compare_directly(self):
        first = _card_events(scrape(events=[goal(player='B'), goal(player='A')]))
        second = _card_events(scrape(events=[goal(player='A'), goal(player='B')]))
        assert first == second

    def test_malformed_events_yield_nothing(self):
        data = scrape()
        data['events'] = 'not a list'
        assert _card_events(data) == []


class TestEarlyCardStale:
    def test_unchanged_card_is_not_stale(self):
        events = _card_events(scrape(events=[goal()]))
        assert _early_card_stale(('1', '0'), events, ('1', '0'), events) is None

    def test_score_change_is_stale(self):
        reason = _early_card_stale(('1', '0'), [], ('1', '1'), [])
        assert reason is not None
        assert '1-0' in reason and '1-1' in reason

    def test_new_event_at_the_same_score_is_stale(self):
        # A red card or a missed penalty changes the card without touching the
        # score, so the score alone cannot decide this.
        before = _card_events(scrape(events=[goal()]))
        after = _card_events(scrape(events=[goal(), goal(type='red_card',
                                                        player='Sent Off')]))
        reason = _early_card_stale(('1', '0'), before, ('1', '0'), after)
        assert reason is not None
        assert 'Sent Off' in reason

    def test_a_vanished_event_alone_is_not_stale(self):
        # Almost always the scraper dropping an event for a poll or two.
        # Reposting on it would delete a correct card, then delete it again
        # when the event came back.
        before = _card_events(scrape(events=[goal(), goal(player='Second')]))
        after = _card_events(scrape(events=[goal()]))
        assert _early_card_stale(('2', '0'), before, ('2', '0'), after) is None

    def test_missing_event_baseline_falls_back_to_score_only(self):
        # stored_events is None for a card posted before that state key
        # existed — a worker resumed mid-match across the upgrade.
        assert _early_card_stale(('1', '0'), None, ('1', '0'), ['anything']) is None
        assert _early_card_stale(('1', '0'), None, ('2', '0'), []) is not None

    def test_score_change_wins_over_an_unchanged_event_list(self):
        reason = _early_card_stale(('0', '0'), [], ('1', '0'), [])
        assert reason is not None and reason.startswith('score')
