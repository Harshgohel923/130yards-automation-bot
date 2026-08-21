"""
The scorer steps: always escapable, and counted against the score.

Two failures this prevents. A team that didn't score has nothing to type, and
"send the word none" is a thing you have to have read — without a button the
step looks like a dead end. And a list that doesn't add up to the score is
nearly always a typo, which is worth catching while the list is still the last
thing typed rather than at the preview, three steps later, where the natural
response to a warning is to post anyway.

Driven with asyncio.run rather than a plugin: requirements-dev.txt is pinned on
purpose, and one helper is cheaper than a new CI dependency.
"""

import asyncio

import pytest

import manual_match as mm
import telegram_bot as tb


def run(coro):
    return asyncio.run(coro)


class Msg:
    """A message that records what was said back."""

    def __init__(self):
        self.said = []
        self.buttons = []

    async def reply_text(self, text, **kw):
        self.said.append(text)
        markup = kw.get('reply_markup')
        if markup is not None and hasattr(markup, 'inline_keyboard'):
            self.buttons += [b.callback_data
                             for row in markup.inline_keyboard for b in row]
        return self

    @property
    def last(self):
        return self.said[-1] if self.said else ''


class Query:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


class Update:
    def __init__(self, query):
        self.callback_query = query
        self.message = None


class Context:
    def __init__(self, home_score, away_score, side='home'):
        self.user_data = {'manual': {
            'owner': '7', 'home_team': 'Arsenal', 'away_team': 'Man City',
            'home_score': home_score, 'away_score': away_score,
            'scorer_side': side,
        }}
        self.chat_data = {}
        self.bot = None

    @property
    def manual(self):
        return self.user_data['manual']


def events(text, team='Arsenal'):
    return mm.parse_scorers(text, team)


# ── The 0-goal step is never a dead end ───────────────────────────────────────

def test_a_team_that_did_not_score_is_told_so():
    msg, ctx = Msg(), Context('0', '2')
    state = run(tb._ask_scorers(msg, ctx, 'home'))
    assert state == tb.M_HOME_SCORERS
    assert "didn't score" in msg.last


def test_every_scorer_step_offers_a_one_tap_exit():
    """Including the one where the team *did* score — a goal with no recorded
    scorer is a real thing."""
    for score in ('0', '3'):
        msg, ctx = Msg(), Context(score, '1')
        run(tb._ask_scorers(msg, ctx, 'home'))
        assert 'scorers:none' in msg.buttons


def test_the_exit_advances_when_there_was_nothing_to_add():
    ctx = Context('0', '2')
    query = Query('scorers:none')
    state = run(tb.scorers_choice(Update(query), ctx))
    assert state == tb.M_AWAY_SCORERS
    assert ctx.manual['home_events'] == []


# ── Counting goals against the score ──────────────────────────────────────────

def test_a_matching_count_moves_straight_on():
    msg, ctx = Msg(), Context('2', '0')
    state = run(tb._scorers_entered(msg, ctx, 'home',
                                    events('23 Saka\n45 Odegaard')))
    assert state == tb.M_AWAY_SCORERS


def test_too_few_goals_holds_the_step():
    msg, ctx = Msg(), Context('2', '0')
    state = run(tb._scorers_entered(msg, ctx, 'home', events('23 Saka')))
    assert state == tb.M_HOME_SCORERS
    assert '1 short' in msg.last


def test_too_many_goals_holds_the_step():
    msg, ctx = Msg(), Context('1', '0')
    state = run(tb._scorers_entered(msg, ctx, 'home',
                                    events('23 Saka\n45 Odegaard')))
    assert state == tb.M_HOME_SCORERS
    assert 'more than that' in msg.last


def test_the_warning_lists_what_it_counted():
    """So the discrepancy can be found by reading, not by recounting."""
    msg, ctx = Msg(), Context('3', '0')
    run(tb._scorers_entered(msg, ctx, 'home', events('23 Saka\n45 Odegaard')))
    assert 'Saka' in msg.last and 'Odegaard' in msg.last


def test_tapping_none_when_the_score_says_goals_is_still_caught():
    """The one-tap exit must not become a one-tap way past the check."""
    ctx = Context('2', '0')
    state = run(tb.scorers_choice(Update(Query('scorers:none')), ctx))
    assert state == tb.M_HOME_SCORERS


# ── What counts, and what doesn't ─────────────────────────────────────────────

def test_a_red_card_is_not_a_goal():
    """A sending-off for a team that didn't score is perfectly normal, and
    counting it would flag every one of them."""
    msg, ctx = Msg(), Context('0', '1')
    state = run(tb._scorers_entered(msg, ctx, 'home', events('55 Rice (red)')))
    assert state == tb.M_AWAY_SCORERS


def test_a_missed_penalty_is_not_a_goal():
    msg, ctx = Msg(), Context('0', '1')
    state = run(tb._scorers_entered(msg, ctx, 'home',
                                    events('55 Saka (miss)')))
    assert state == tb.M_AWAY_SCORERS


def test_penalties_and_own_goals_do_count():
    """Both put a number on the scoreboard, so both have to count or every
    match with one would be flagged."""
    msg, ctx = Msg(), Context('2', '0')
    state = run(tb._scorers_entered(
        msg, ctx, 'home', events('23 Havertz (pen)\n45 Dias (og)')))
    assert state == tb.M_AWAY_SCORERS


def test_a_mix_counts_only_the_goals():
    msg, ctx = Msg(), Context('1', '0')
    state = run(tb._scorers_entered(
        msg, ctx, 'home',
        events('23 Saka\n55 Rice (red)\n70 Odegaard (miss)')))
    assert state == tb.M_AWAY_SCORERS


# ── Answering the warning ─────────────────────────────────────────────────────

def test_accepting_a_mismatch_carries_on():
    """It asks rather than refuses: a scorer can be genuinely unknown, and a
    card can legitimately show fewer names than goals."""
    ctx = Context('2', '0')
    ctx.manual['home_events'] = events('23 Saka')
    state = run(tb.scorers_choice(Update(Query('scorers:ok')), ctx))
    assert state == tb.M_AWAY_SCORERS


def test_redo_re_asks_the_same_team():
    ctx = Context('2', '0')
    state = run(tb.scorers_choice(Update(Query('scorers:redo')), ctx))
    assert state == tb.M_HOME_SCORERS


def test_the_away_step_leads_to_the_background_not_a_third_team():
    msg, ctx = Msg(), Context('0', '1')
    ctx.manual['scorer_side'] = 'away'
    state = run(tb._scorers_entered(msg, ctx, 'away',
                                    events('23 Haaland', 'Man City')))
    assert state == tb.M_BACKGROUND


# ── The preview keeps its own check ───────────────────────────────────────────

def test_the_preview_still_warns_about_an_accepted_mismatch():
    """Accepting the warning mid-flow shouldn't erase it from the readback —
    the last look before posting should still say what it is."""
    data = {'home_team': 'Arsenal', 'away_team': 'Man City',
            'home_score': '3', 'away_score': '0',
            'competition': 'Premier League', 'event_type': 'FT',
            'when': mm.parse_date('21/08/2026')}
    scraper_data = mm.build_scraper_data(
        home_team='Arsenal', away_team='Man City',
        home_score='3', away_score='0', competition='Premier League',
        when=data['when'], event_type='FT',
        home_events=events('23 Saka'), away_events=[])
    assert '⚠️' in tb._summary(data, scraper_data, styled=False, waiting=[])


@pytest.mark.parametrize('side, state', [('home', tb.M_HOME_SCORERS),
                                         ('away', tb.M_AWAY_SCORERS)])
def test_each_side_maps_to_its_own_state(side, state):
    assert tb.SCORER_STATE[side] == state
