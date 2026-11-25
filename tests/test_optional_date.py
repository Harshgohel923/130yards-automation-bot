"""
The date on a /card card is optional.

The date step offers a "No date on the card" button and accepts `skip` (and a
few synonyms) typed; either way the card comes out with the top-right corner
empty, exactly like a scraped match.
"""

import asyncio
import types
from datetime import datetime, timezone

import pytest

import manual_match as mm
import telegram_bot as tb


def run(coro):
    return asyncio.run(coro)


class Msg:
    def __init__(self, text=''):
        self.text = text
        self.said = []
        self.buttons = []

    async def reply_text(self, text, **kw):
        self.said.append(text)
        markup = kw.get('reply_markup')
        if markup is not None and hasattr(markup, 'inline_keyboard'):
            self.buttons += [b.callback_data
                             for row in markup.inline_keyboard for b in row]
        return self


class Query:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


def _context():
    return types.SimpleNamespace(user_data={'manual': {
        'home_team': 'Arsenal', 'away_team': 'Chelsea',
        'home_score': '1', 'away_score': '0', 'event_type': 'FT',
        'competition': 'Premier League'}}, chat_data={})


# ── The step itself ──────────────────────────────────────────────────────────

def test_the_date_step_offers_a_no_date_button():
    msg = Msg()
    assert run(tb._ask_date(msg)) == tb.M_DATE
    assert 'manual:nodate' in msg.buttons
    assert 'optional' in msg.said[-1]


def test_the_button_skips_the_date_and_moves_on():
    ctx = _context()
    query = Query('manual:nodate')
    update = types.SimpleNamespace(callback_query=query)
    state = run(tb.card_no_date(update, ctx))
    assert state == tb.SCORER_STATE['home']
    assert ctx.user_data['manual']['when'] is None


@pytest.mark.parametrize('typed', ['skip', 'Skip', 'none', 'no date', '-'])
def test_typing_skip_does_the_same(typed):
    ctx = _context()
    update = types.SimpleNamespace(message=Msg(typed))
    state = run(tb.card_date(update, ctx))
    assert state == tb.SCORER_STATE['home']
    assert ctx.user_data['manual']['when'] is None


def test_a_real_date_still_works():
    ctx = _context()
    update = types.SimpleNamespace(message=Msg('21/08/2026'))
    run(tb.card_date(update, ctx))
    assert ctx.user_data['manual']['when'] == datetime(2026, 8, 21,
                                                       tzinfo=timezone.utc)


def test_the_date_state_handles_the_button():
    src = open(tb.__file__, encoding='utf-8').read()
    block = src[src.index('M_DATE:         ['):]
    block = block[:block.index('],')]
    assert 'card_no_date' in block and 'manual:nodate' in block


# ── What it produces ─────────────────────────────────────────────────────────

def _build(when):
    return mm.build_scraper_data(
        home_team='Arsenal', away_team='Chelsea', home_score='1',
        away_score='0', competition='Premier League', when=when,
        event_type='FT', home_events=[], away_events=[])


def test_no_date_means_no_card_date():
    sample = _build(None)['matchSample']
    assert 'card_date' not in sample and 'date_utc' not in sample
    assert sample['match_id'].startswith('manual-')


def test_a_date_still_lands_on_the_card():
    sample = _build(datetime(2026, 8, 21, tzinfo=timezone.utc))['matchSample']
    assert sample['card_date'] == '21 AUG 2026'
    assert sample['match_id'] == 'manual-20260821-ARSCHE'


def test_the_readback_says_no_date():
    data = dict(_context().user_data['manual'], when=None)
    text = tb._summary(data, _build(None), styled=False, waiting=[])
    assert 'no date' in text


def test_wants_no_date_ignores_real_dates():
    assert mm.wants_no_date('skip')
    assert not mm.wants_no_date('21/08/2026')
    assert not mm.wants_no_date('')
