"""
Choosing a carousel's slide order over Telegram before it posts.

The dispatcher sends the matches in kickoff order with a button each; taps
build the order one slide at a time. It posts once nobody has tapped for a
minute — picks first, the rest as listed — or as soon as the order is complete.
"""

from datetime import datetime, timedelta, timezone

import pytest

import carousel
import carousel_order as co


IDS = ['m1', 'm2', 'm3']
LABELS = ['A vs B', 'C vs D', 'E vs F']


# ── Buttons and arithmetic ───────────────────────────────────────────────────

def test_callback_round_trips_and_fits_telegram():
    data = co.callback('ab12cd34', [9, 0, 3, 1, 2, 4, 5, 6, 7], done=True)
    assert len(data.encode()) <= 64
    assert co.parse_callback(data) == ('ab12cd34', [9, 0, 3, 1, 2, 4, 5, 6, 7], True)
    assert co.parse_callback('co:ab12cd34:') == ('ab12cd34', [], False)


@pytest.mark.parametrize('bad', ['', 'ec:x:1', 'co::1', 'co:q:1a', 'co:q:11'])
def test_malformed_callbacks_are_rejected(bad):
    assert co.parse_callback(bad) is None


def test_picks_go_first_and_the_rest_keep_their_order():
    assert co.apply_order(IDS, []) == IDS
    assert co.apply_order(IDS, [2]) == ['m3', 'm1', 'm2']
    assert co.apply_order(IDS, [1, 2]) == ['m2', 'm3', 'm1']


def test_the_first_keyboard_offers_every_match_and_keep_as_is():
    opts = co.keyboard('q', LABELS, [])
    assert [d for _l, d in opts] == ['co:q:0', 'co:q:1', 'co:q:2', 'co:q:=']
    assert opts[0][0] == '1. A vs B'


def test_later_keyboards_offer_what_is_left_plus_exits():
    opts = co.keyboard('q', LABELS, [2])
    assert [d for _l, d in opts] == ['co:q:20', 'co:q:21', 'co:q:2=', 'co:q:']
    assert opts[0][0].startswith('2. ')


def test_one_match_left_completes_the_order():
    assert co.is_complete(3, [2, 0], False)
    assert not co.is_complete(3, [2], False)
    assert co.is_complete(3, [], True)


# ── A fake Cloudinary store and clock ────────────────────────────────────────

class World:
    def __init__(self, monkeypatch):
        self.t = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
        self.store = {}
        self.sent, self.edits = [], []
        self.script = {}          # seconds since start → callback to tap
        self.start = self.t
        monkeypatch.setattr(co, '_now', lambda: self.t)
        monkeypatch.setattr(co, 'read_state',
                            lambda qid: dict(self.store[qid]) if qid in self.store else None)

        def write(qid, state):
            self.store[qid] = dict(state, updated_at=co._iso(self.t))
        monkeypatch.setattr(co, 'write_state', write)

    def send_choice(self, text, options):
        self.sent.append((text, options))
        return 42

    def edit_message(self, message_id, text):
        self.edits.append((message_id, text))

    def sleep(self, secs):
        before = (self.t - self.start).total_seconds()
        self.t += timedelta(seconds=secs)
        after = (self.t - self.start).total_seconds()
        for at, data in sorted(self.script.items()):
            if before < at <= after:
                co.record_tap(data)

    def choose(self):
        return co.choose_order('sat', 'carousel/2026-11-25/sat', IDS, LABELS,
                               send_choice=self.send_choice,
                               edit_message=self.edit_message, sleep=self.sleep)

    @property
    def qid(self):
        return co.question_id('carousel/2026-11-25/sat')

    def waited(self):
        return (self.t - self.start).total_seconds()


# ── The dispatcher's wait ────────────────────────────────────────────────────

def test_no_answer_posts_as_listed_after_a_minute(monkeypatch):
    w = World(monkeypatch)
    assert w.choose() == IDS
    assert len(w.sent) == 1 and 'Which order' in w.sent[0][0]
    assert co.IDLE_SECS <= w.waited() < co.IDLE_SECS + co.POLL_SECS + 1
    (mid, text), = w.edits
    assert mid == 42 and 'kept as listed' in text
    assert w.store[w.qid]['final'] and w.store[w.qid]['source'] == 'default'


def test_a_full_order_posts_straight_away(monkeypatch):
    w = World(monkeypatch)
    q = w.qid
    w.script = {10: f'co:{q}:2', 20: f'co:{q}:21'}     # E vs F, then C vs D
    assert w.choose() == ['m3', 'm2', 'm1']
    assert w.waited() < 30
    assert w.store[q]['source'] == 'you'
    assert not w.edits                                 # the bot already said so


def test_each_tap_restarts_the_minute(monkeypatch):
    w = World(monkeypatch)
    q = w.qid
    w.script = {50: f'co:{q}:1'}                       # one pick, then silence
    assert w.choose() == ['m2', 'm1', 'm3']
    assert w.waited() >= 50 + co.IDLE_SECS
    assert 'the rest as listed' in w.edits[0][1]
    assert w.store[q]['source'] == 'partial'


def test_keep_this_order_button_posts_at_once(monkeypatch):
    w = World(monkeypatch)
    w.script = {5: f'co:{w.qid}:='}
    assert w.choose() == IDS
    assert w.waited() < 15


def test_a_retry_reuses_the_decision_without_asking_again(monkeypatch):
    w = World(monkeypatch)
    w.store[w.qid] = {'group': 'sat', 'match_ids': IDS, 'labels': LABELS,
                      'final': True, 'order': ['m3', 'm1', 'm2'], 'source': 'you'}
    assert w.choose() == ['m3', 'm1', 'm2']
    assert not w.sent and w.waited() == 0


def test_a_single_match_is_never_asked_about(monkeypatch):
    w = World(monkeypatch)
    assert co.choose_order('sat', 'r', ['m1'], ['A vs B'],
                           send_choice=w.send_choice,
                           edit_message=w.edit_message, sleep=w.sleep) == ['m1']
    assert not w.sent


def test_endless_tapping_is_capped(monkeypatch):
    w = World(monkeypatch)
    q = w.qid
    # Start over every 30 s for ever: never complete, never idle.
    w.script = {s: f'co:{q}:' for s in range(30, 2000, 30)}
    w.choose()
    assert co.MAX_WAIT_SECS <= w.waited() < co.MAX_WAIT_SECS + 2 * co.POLL_SECS


# ── The bot's side ───────────────────────────────────────────────────────────

def test_a_tap_after_the_decision_is_too_late(monkeypatch):
    w = World(monkeypatch)
    w.store[w.qid] = {'group': 'sat', 'match_ids': IDS, 'labels': LABELS,
                      'final': True, 'order': IDS, 'source': 'default'}
    text, buttons = co.record_tap(f'co:{w.qid}:2')
    assert text.startswith('Too late') and buttons is None
    assert w.store[w.qid]['order'] == IDS


def test_a_tap_on_an_unknown_question_has_expired(monkeypatch):
    World(monkeypatch)
    text, buttons = co.record_tap('co:deadbeef:1')
    assert 'expired' in text and buttons is None


def test_a_mid_way_tap_shows_progress_and_the_next_buttons(monkeypatch):
    w = World(monkeypatch)
    w.store[w.qid] = {'group': 'sat', 'match_ids': IDS, 'labels': LABELS,
                      'picks': [], 'final': False}
    text, buttons = co.record_tap(f'co:{w.qid}:1')
    assert '1. C vs D' in text and 'slide 2' in text
    assert buttons[0][1] == f'co:{w.qid}:10'
    assert w.store[w.qid]['picks'] == [1]


# ── Wired into publish_group ─────────────────────────────────────────────────

def test_publish_group_posts_in_the_chosen_order(monkeypatch):
    manifests = {m: {'match_id': m, 'home_team': m, 'away_team': 'X',
                     'kickoff_utc': f'2026-11-25T1{i}:00:00Z',
                     'image_url': f'https://img/{m}.png'}
                 for i, m in enumerate(IDS)}
    entries = [{'match_id': m, 'carousel_group': 'sat',
                'kickoff_utc': manifests[m]['kickoff_utc']} for m in IDS]
    posted = {}
    monkeypatch.setattr(carousel, 'posted_marker', lambda g, e=None: None)
    monkeypatch.setattr(carousel, 'read_manifests', lambda g, e=None: manifests)
    monkeypatch.setattr(carousel, 'generate_group_caption', lambda ms: 'cap')
    monkeypatch.setattr(carousel, 'mark_group_posted', lambda *a, **k: None)
    monkeypatch.setattr(carousel, 'send_music_reminder', lambda *a, **k: None)
    monkeypatch.setattr(carousel, 'post_carousel_to_instagram',
                        lambda urls, cap: posted.setdefault('urls', urls) and 'ig')
    monkeypatch.setattr(co, 'choose_order',
                        lambda *a, **k: ['m3', 'm1', 'm2'])
    carousel.publish_group('sat', entries)
    assert posted['urls'] == ['https://img/m3.png', 'https://img/m1.png',
                              'https://img/m2.png']


def test_a_broken_question_never_costs_the_post(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('cloudinary down')
    monkeypatch.setattr(co, 'choose_order', boom)
    ordered = [{'match_id': m} for m in IDS]
    assert carousel._ask_for_order('sat', [], ordered) == ordered


def test_the_bot_handler_edits_the_message_with_the_next_buttons(monkeypatch):
    import asyncio
    import types
    import telegram_bot as tb

    edits = []

    class Query:
        data = 'co:q:1'
        message = None

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, reply_markup=None):
            edits.append((text, reply_markup))

    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(co, 'record_tap',
                        lambda data: ('picked', [('2. A vs B', 'co:q:10')]))
    update = types.SimpleNamespace(callback_query=Query())
    asyncio.run(tb.carousel_order_tap(update, None))
    (text, markup), = edits
    assert text == 'picked'
    assert markup.inline_keyboard[0][0].callback_data == 'co:q:10'


def test_the_bot_registers_the_order_buttons():
    src = open(__import__('telegram_bot').__file__, encoding='utf-8').read()
    assert "CallbackQueryHandler(carousel_order_tap, pattern=r'^co:')" in src
