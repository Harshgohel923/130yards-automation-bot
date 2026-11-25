"""
A half-time card on request for fixtures with post_ht off.

At the whistle the worker asks on Telegram; the bot records the tap on
Cloudinary; the worker reads it during the interval and posts on a yes. An
unanswered question is closed when the second half starts.
"""

from datetime import datetime, timedelta, timezone

import pytest

import ht_optin
import main
import matchlog
from tests.helpers import goal, scrape


MATCH_ID = '99000003'
GOAL = [goal(team='A', player='Scorer', minute='23')]


def _ht():
    return scrape(minute='45', fs=('1', '0'), hts=('1', '0'), events=GOAL)


def _2h():
    return scrape(minute='50', fs=('1', '0'), hts=('1', '0'), events=GOAL)


class FeedExhausted(Exception):
    pass


@pytest.fixture
def run(monkeypatch):
    def go(scrapes, post_ht=False, answers=None, group=None):
        """answers: poll number (1-based reads) → what the bot has recorded."""
        rec = {'asked': [], 'edits': [], 'posts': [], 'reads': 0, 'posted': set()}
        answers = answers or {}

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
            '_save_match_data': lambda *a, **k: None,
            'send_alert': lambda *a, **k: None,
            'send_music_reminder': lambda *a, **k: None,
            'is_event_posted': lambda mid, et: (mid, et) in rec['posted'],
            'mark_event_posted': lambda mid, et: rec['posted'].add((mid, et)),
            'send_choice': lambda text, options, **k: rec['asked'].append(
                (text, options)) or 777,
            'edit_message': lambda mid, text: rec['edits'].append((mid, text)),
            '_post_slides': lambda e, et, d: rec['posts'].append(et) or (['c'], 'ig'),
        }.items():
            monkeypatch.setattr(main, name, fn)

        def read_answer(match_id):
            rec['reads'] += 1
            return answers.get(rec['reads'])
        monkeypatch.setattr(ht_optin, 'read_answer', read_answer)

        feed = iter(scrapes)

        def fake_get(url):
            try:
                return next(feed)
            except StopIteration:
                raise FeedExhausted
        monkeypatch.setattr(main, 'get_match_data', fake_get)
        monkeypatch.setattr(main.time, 'sleep', lambda s: None)

        main.MATCH_STATE.clear()
        entry = {'match_id': MATCH_ID, 'scraper_url': 'x', 'home_team': 'Home',
                 'away_team': 'Away', 'post_ht': post_ht,
                 **({'carousel_group': group} if group else {}),
                 'kickoff_utc': (datetime.now(timezone.utc) - timedelta(minutes=50))
                                .isoformat()}
        try:
            main.match_worker(entry)
        except FeedExhausted:
            pass
        rec['state'] = main.MATCH_STATE[MATCH_ID]
        return rec
    return go


def test_it_asks_once_at_the_whistle(run):
    rec = run([_ht()] * 4)
    assert len(rec['asked']) == 1
    text, options = rec['asked'][0]
    assert 'half time, 1-0' in text
    assert [d for _l, d in options] == [f'ht:{MATCH_ID}:post', f'ht:{MATCH_ID}:skip']
    assert rec['posts'] == []


def test_a_yes_posts_the_ht_card(run):
    rec = run([_ht()] * 4, answers={2: {'answer': 'post', 'style': 'photo'}})
    assert rec['posts'] == ['HT']
    assert rec['state']['ht_posted'] is True


def test_a_no_posts_nothing_and_stops_asking_cloudinary(run):
    rec = run([_ht()] * 5, answers={1: {'answer': 'skip'}})
    assert rec['posts'] == []
    assert rec['reads'] == 1


def test_unanswered_question_is_closed_when_play_resumes(run):
    rec = run([_ht(), _ht(), _2h(), _2h()])
    assert rec['posts'] == []
    (mid, text), = rec['edits']
    assert mid == 777 and 'second half has started' in text


def test_an_answered_question_is_left_alone_after_ht(run):
    rec = run([_ht(), _ht(), _2h()], answers={1: {'answer': 'skip'}})
    assert rec['edits'] == []


def test_a_post_ht_fixture_is_never_asked(run):
    rec = run([_ht()] * 2, post_ht=True)
    assert rec['asked'] == [] and rec['posts'] == ['HT']


def test_a_carousel_group_match_is_never_asked(run):
    rec = run([_ht()] * 3 + [_2h()], group='sat')
    assert rec['asked'] == [] and rec['posts'] == [] and rec['edits'] == []
    assert rec['reads'] == 0                       # Cloudinary never consulted


def test_no_question_outside_half_time(run):
    rec = run([_2h()] * 3)
    assert rec['asked'] == [] and rec['edits'] == []


def test_standard_template_wins_over_an_uploaded_photo(monkeypatch):
    main.MATCH_STATE.clear()
    main.MATCH_STATE[MATCH_ID] = {'ht_optin_style': 'template'}
    used = {}
    monkeypatch.setattr(main, 'fetch_match_photo',
                        lambda *a: used.setdefault('photo_fetched', True) and 'p.png')
    monkeypatch.setattr(main, 'generate_scorecard',
                        lambda *a, **k: used.setdefault('card', 'template'))
    monkeypatch.setattr(main, 'generate_overlay_scorecard',
                        lambda *a, **k: used.setdefault('card', 'overlay'))
    monkeypatch.setattr(matchlog, 'log', lambda *a, **k: None)
    entry = {'match_id': MATCH_ID, 'home_team': 'Home', 'away_team': 'Away'}
    main._generate_card(entry, 'HT', {'matchSample': {}})
    assert used == {'card': 'template'}           # photo never even looked up

    # The FT card of the same match still uses a photo if there is one.
    used.clear()
    main._generate_card(entry, 'FT', {'matchSample': {}})
    assert used.get('card') == 'overlay'


# ── The bot's side ───────────────────────────────────────────────────────────

def test_callbacks_round_trip():
    assert ht_optin.parse_callback('ht:54577212:post') == ('54577212', 'post')
    assert ht_optin.parse_callback('ht:54577212:skip') == ('54577212', 'skip')
    for bad in ('ht:1:maybe', 'co:1:post', 'ht::post', 'ht:1'):
        assert ht_optin.parse_callback(bad) is None
    assert len(ht_optin.callback('manual-20261125-ARSCHE', 'post').encode()) <= 64


def test_no_thanks_is_recorded(monkeypatch):
    written = []
    monkeypatch.setattr(ht_optin, 'write_answer',
                        lambda mid, ans, style=None: written.append((mid, ans)))
    assert 'no half-time card' in ht_optin.record_skip('ht:42:skip')
    assert written == [('42', 'skip')]


class _Msg:
    def __init__(self, text='🙋 Home vs Away — half time, 1-0.\n\nMore…'):
        self.text = text
        self.replies = []
        self.photo, self.document = [], None

    async def reply_text(self, text, **k):
        self.replies.append(text)


class _Query:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **k):
        self.edits.append((text, reply_markup))


@pytest.fixture
def bot(monkeypatch):
    import types
    import telegram_bot as tb
    written = []
    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(tb, '_touch_session', lambda: None)
    monkeypatch.setattr(ht_optin, 'write_answer',
                        lambda mid, ans, style=None: written.append((mid, ans, style)))
    ctx = types.SimpleNamespace(user_data={}, chat_data={})
    return tb, ctx, written


def test_post_asks_for_a_background_and_records_nothing_yet(bot):
    import asyncio, types
    tb, ctx, written = bot
    q = _Query('ht:42:post')
    state = asyncio.run(tb.ht_post_tap(types.SimpleNamespace(callback_query=q), ctx))
    assert state == tb.HT_BG
    text, markup = q.edits[0]
    assert text.startswith('🙋 Home vs Away — half time, 1-0.')
    assert 'background photo' in text
    assert markup.inline_keyboard[0][0].callback_data == 'htbg:42'
    assert written == []                          # the worker must wait


def test_the_template_button_records_post_on_the_template(bot):
    import asyncio, types
    tb, ctx, written = bot
    asyncio.run(tb.ht_post_tap(types.SimpleNamespace(callback_query=_Query('ht:42:post')), ctx))
    q = _Query('htbg:42')
    state = asyncio.run(tb.ht_bg_template(types.SimpleNamespace(callback_query=q), ctx))
    assert state == tb.ConversationHandler.END
    assert written == [('42', 'post', 'template')]
    assert 'standard template' in q.edits[0][0]


def test_a_photo_is_uploaded_as_the_ht_photo_then_recorded(bot, monkeypatch):
    import asyncio, types
    tb, ctx, written = bot
    asyncio.run(tb.ht_post_tap(types.SimpleNamespace(callback_query=_Query('ht:42:post')), ctx))
    uploads = []

    async def fake_upload(msg, context, match, event_type, ref, **k):
        uploads.append((match['match_id'], event_type))
        return True

    monkeypatch.setattr(tb, '_upload_photo', fake_upload)
    monkeypatch.setattr(tb, '_photo_ref', lambda msg: ('file-id', 1000))
    monkeypatch.setattr(tb, '_load_matches', lambda: [])
    msg = _Msg()
    state = asyncio.run(tb.ht_bg_photo(types.SimpleNamespace(message=msg), ctx))
    assert state == tb.ConversationHandler.END
    assert uploads == [('42', 'HT')]
    assert written == [('42', 'post', 'photo')]
    assert 'with your photo' in msg.replies[-1]


def test_a_failed_upload_records_nothing_and_waits_for_another(bot, monkeypatch):
    import asyncio, types
    tb, ctx, written = bot
    asyncio.run(tb.ht_post_tap(types.SimpleNamespace(callback_query=_Query('ht:42:post')), ctx))

    async def failed_upload(*a, **k):
        return False

    monkeypatch.setattr(tb, '_upload_photo', failed_upload)
    monkeypatch.setattr(tb, '_photo_ref', lambda msg: ('file-id', 1000))
    monkeypatch.setattr(tb, '_load_matches', lambda: [])
    state = asyncio.run(tb.ht_bg_photo(types.SimpleNamespace(message=_Msg()), ctx))
    assert state == tb.HT_BG and written == []


def test_a_photo_after_half_time_is_turned_away(bot, monkeypatch):
    import asyncio, types
    tb, ctx, written = bot
    asyncio.run(tb.ht_post_tap(types.SimpleNamespace(callback_query=_Query('ht:42:post')), ctx))
    ctx.user_data['ht_bg']['since'] -= ht_optin.PHOTO_WINDOW_SECS + 1
    msg = _Msg()
    state = asyncio.run(tb.ht_bg_photo(types.SimpleNamespace(message=msg), ctx))
    assert state == tb.ConversationHandler.END
    assert written == [] and 'Half time is over' in msg.replies[-1]


def test_the_conversation_is_wired_ahead_of_the_photo_flow():
    import telegram_bot as tb
    src = open(tb.__file__, encoding='utf-8').read()
    assert src.index('app.add_handler(ht_conv)') < src.index('app.add_handler(conv)')
    assert "CallbackQueryHandler(ht_post_tap, pattern=r'^ht:[^:]+:post$')" in src
    assert "CallbackQueryHandler(ht_skip_tap, pattern=r'^ht:[^:]+:skip$')" in src
