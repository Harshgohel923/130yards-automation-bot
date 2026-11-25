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
    def go(scrapes, post_ht=False, answers=None):
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
    rec = run([_ht()] * 4, answers={2: 'post'})
    assert rec['posts'] == ['HT']
    assert rec['state']['ht_posted'] is True


def test_a_no_posts_nothing_and_stops_asking_cloudinary(run):
    rec = run([_ht()] * 5, answers={1: 'skip'})
    assert rec['posts'] == []
    assert rec['reads'] == 1


def test_unanswered_question_is_closed_when_play_resumes(run):
    rec = run([_ht(), _ht(), _2h(), _2h()])
    assert rec['posts'] == []
    (mid, text), = rec['edits']
    assert mid == 777 and 'second half has started' in text


def test_an_answered_question_is_left_alone_after_ht(run):
    rec = run([_ht(), _ht(), _2h()], answers={1: 'skip'})
    assert rec['edits'] == []


def test_a_post_ht_fixture_is_never_asked(run):
    rec = run([_ht()] * 2, post_ht=True)
    assert rec['asked'] == [] and rec['posts'] == ['HT']


def test_no_question_outside_half_time(run):
    rec = run([_2h()] * 3)
    assert rec['asked'] == [] and rec['edits'] == []


# ── The bot's side ───────────────────────────────────────────────────────────

def test_callbacks_round_trip():
    assert ht_optin.parse_callback('ht:54577212:post') == ('54577212', 'post')
    assert ht_optin.parse_callback('ht:54577212:skip') == ('54577212', 'skip')
    for bad in ('ht:1:maybe', 'co:1:post', 'ht::post', 'ht:1'):
        assert ht_optin.parse_callback(bad) is None
    assert len(ht_optin.callback('manual-20261125-ARSCHE', 'post').encode()) <= 64


def test_a_tap_is_recorded(monkeypatch):
    written = []
    monkeypatch.setattr(ht_optin, 'write_answer',
                        lambda mid, ans: written.append((mid, ans)))
    assert 'Posting the half-time card' in ht_optin.record_tap('ht:42:post')
    assert written == [('42', 'post')]


def test_the_bot_handler_records_and_keeps_the_question_line(monkeypatch):
    import asyncio
    import types
    import telegram_bot as tb
    edits = []

    class Query:
        data = 'ht:42:post'
        message = types.SimpleNamespace(text='🙋 Home vs Away — half time, 1-0.\n\nMore…')

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **k):
            edits.append(text)

    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(ht_optin, 'write_answer', lambda *a: None)
    asyncio.run(tb.ht_optin_tap(types.SimpleNamespace(callback_query=Query()), None))
    (text,) = edits
    assert text.startswith('🙋 Home vs Away — half time, 1-0.')
    assert 'Posting the half-time card' in text

    src = open(tb.__file__, encoding='utf-8').read()
    assert "CallbackQueryHandler(ht_optin_tap, pattern=r'^ht:[^:]+:(post|skip)$')" in src
