"""
"The scoreline has changed since your photo — new photo, or the old one?"

From 35' (HT card) / 80' (FT card), once a background photo is there, the
worker notes who is ahead. If that changes before the card goes, it asks, and
holds the card — early posts and corrections too — for: "Post the older one"
(now), no answer (2 min, then the old photo), or "Upload new photo" (up to 10
min for it, then the old photo).
"""

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

import main
import matchlog
import story_photo as sp
from tests.helpers import goal, scrape


MATCH_ID = '99000004'
ENTRY = {'match_id': MATCH_ID, 'home_team': 'Home', 'away_team': 'Away'}


# ── Pure pieces ──────────────────────────────────────────────────────────────

def test_who_is_ahead():
    assert sp.leader('2', '1') == 'home'
    assert sp.leader(0, 3) == 'away'
    assert sp.leader('1', '1') == 'level'
    assert sp.leader('x', '1') is None


def test_describe():
    assert sp.describe('Arsenal', 'Chelsea', ('1', '1')) == 'Arsenal 1-1 Chelsea (level)'
    assert sp.describe('Arsenal', 'Chelsea', ('2', '1')) == 'Arsenal 2-1 Chelsea (Arsenal ahead)'


def test_callbacks_round_trip_and_fit():
    data = sp.callback('54577212', 'FT', 1764100000, 'new')
    assert sp.parse_callback(data) == ('54577212', 'FT', 1764100000, 'new')
    assert len(data.encode()) <= 64
    for bad in ('st:1:XX:5:new', 'st:1:FT:x:new', 'st:1:FT:5:maybe', 'ht:1:FT:5:new'):
        assert sp.parse_callback(bad) is None


# ── A fake Cloudinary state, a fake clock, a fake Telegram ───────────────────

@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(remote={}, asked=[], edits=[], photo=True, t=1_764_100_000.0)
    monkeypatch.setattr(sp, 'read_state', lambda mid, m: dict(w.remote[m]) if m in w.remote else None)

    def write(mid, m, state):
        w.remote[m] = dict(state)
    monkeypatch.setattr(sp, 'write_state', write)
    monkeypatch.setattr(main, 'match_photo_exists', lambda mid, m: w.photo)
    monkeypatch.setattr(main, 'send_choice',
                        lambda text, options, **k: w.asked.append((text, options)) or 555)
    monkeypatch.setattr(main, 'edit_message', lambda mid, text: w.edits.append((mid, text)))
    monkeypatch.setattr(main, '_save_state', lambda: None)
    for name in ('log', 'warn'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)
    monkeypatch.setattr(main.time, 'time', lambda: w.t)
    main.MATCH_STATE.clear()
    main.MATCH_STATE[MATCH_ID] = {}
    return w


def test_nothing_is_noted_until_there_is_a_photo(world):
    world.photo = False
    main._watch_story(ENTRY, 'FT', ('1', '0'))
    assert main.MATCH_STATE[MATCH_ID] == {}


def test_extending_a_lead_does_not_ask(world):
    main._watch_story(ENTRY, 'FT', ('1', '0'))
    main._watch_story(ENTRY, 'FT', ('2', '0'))
    assert world.asked == []


def test_an_equaliser_asks_once_with_before_and_now(world):
    main._watch_story(ENTRY, 'FT', ('1', '0'))
    main._watch_story(ENTRY, 'FT', ('1', '1'))
    main._watch_story(ENTRY, 'FT', ('1', '1'))
    (text, options), = world.asked
    assert 'Then: Home 1-0 Away (Home ahead)' in text
    assert 'Now: Home 1-1 Away (level)' in text
    assert [d.rsplit(':', 1)[1] for _l, d in options] == ['new', 'old']


def test_a_second_change_while_asking_does_not_ask_again(world):
    main._watch_story(ENTRY, 'FT', ('1', '0'))
    main._watch_story(ENTRY, 'FT', ('1', '1'))
    main._watch_story(ENTRY, 'FT', ('1', '2'))          # comeback, question still open
    assert len(world.asked) == 1


def _asked(world):
    main._watch_story(ENTRY, 'FT', ('1', '0'))
    main._watch_story(ENTRY, 'FT', ('1', '1'))
    return main.MATCH_STATE[MATCH_ID]['story_FT']['q']['qid']


def test_no_question_means_go(world):
    assert main._story_ready(ENTRY, 'FT') is True


def test_held_for_two_minutes_then_the_old_photo(world):
    _asked(world)
    world.t += 60
    assert main._story_ready(ENTRY, 'FT') is False
    world.t += 61
    assert main._story_ready(ENTRY, 'FT') is True
    assert world.edits == [(555, sp.resolved_text('no-answer'))]
    assert world.remote['FT']['closed'] is True        # late taps say "too late"


def test_post_the_older_one_goes_at_once(world):
    qid = _asked(world)
    world.remote['FT'].update(answer='old', answered_ts=world.t)
    assert main._story_ready(ENTRY, 'FT') is True
    assert world.edits == []                           # the bot already said so


def test_upload_new_waits_up_to_ten_minutes(world):
    _asked(world)
    world.remote['FT'].update(answer='new', answered_ts=world.t + 30)
    world.t += 9 * 60
    assert main._story_ready(ENTRY, 'FT') is False
    world.t += 2 * 60
    assert main._story_ready(ENTRY, 'FT') is True
    assert world.edits == [(555, sp.resolved_text('no-photo'))]


def test_a_new_photo_releases_the_card(world):
    _asked(world)
    world.remote['FT'].update(answer='new', answered_ts=world.t, uploaded=True)
    assert main._story_ready(ENTRY, 'FT') is True
    assert main.MATCH_STATE[MATCH_ID]['story_FT']['q']['resolved'] == 'uploaded'


def test_an_answer_to_an_older_question_does_not_count(world):
    qid = _asked(world)
    world.remote['FT'] = {'qid': qid - 500, 'answer': 'old'}
    assert main._story_ready(ENTRY, 'FT') is False


# ── The real worker loop ─────────────────────────────────────────────────────

class FeedExhausted(Exception):
    pass


@pytest.fixture
def loop(world, monkeypatch):
    rec = {'posts': [], 'deleted': []}
    for name, fn in {
        'get_crest_url': lambda *a, **k: 'crest',
        'upsert_match': lambda e: None,
        'enrich_with_desktop': lambda *a, **k: None,
        '_run_event_photos': lambda *a, **k: None,
        '_clarify_staged_photos': lambda *a, **k: None,
        '_photo_reminder': lambda *a, **k: None,
        '_clear_event_photos': lambda *a, **k: None,
        '_save_match_data': lambda *a, **k: None,
        '_archive_match_data': lambda *a, **k: None,
        'send_alert': lambda *a, **k: None,
        'send_music_reminder': lambda *a, **k: None,
        'is_event_posted': lambda *a: False,
        'mark_event_posted': lambda *a: None,
        '_post_slides': lambda e, et, d: rec['posts'].append(et) or (['c'], 'ig'),
        '_delete_early_post': lambda e, et: rec['deleted'].append(et),
    }.items():
        monkeypatch.setattr(main, name, fn)
    for name in ('start', 'error', 'finish'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)
    monkeypatch.setattr(main.time, 'sleep', lambda s: setattr(world, 't', world.t + s))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(world.t, timezone.utc)
    monkeypatch.setattr(main, 'datetime', Clock)

    def go(scrapes):
        feed = iter(scrapes)

        def get(url):
            try:
                return next(feed)
            except StopIteration:
                raise FeedExhausted
        monkeypatch.setattr(main, 'get_match_data', get)
        main.MATCH_STATE.clear()
        kickoff = datetime.fromtimestamp(world.t, timezone.utc) - timedelta(minutes=80)
        try:
            main.match_worker(dict(ENTRY, scraper_url='x', kickoff_utc=kickoff.isoformat()))
        except FeedExhausted:
            pass
        return rec
    return go


H = [goal(team='A', player='Scorer', minute='20')]
HA = H + [goal(team='B', player='Leveller', minute='86')]


def _2h(minute, fs, events):
    return scrape(minute=minute, fs=fs, hts=('1', '0'), events=events)


def test_a_late_equaliser_holds_the_early_ft_card_two_minutes(loop, world):
    rec = loop([_2h('80', ('1', '0'), H),     # photo noted: Home ahead
                _2h('86', ('1', '1'), HA),    # level — asked
                _2h('90', ('1', '1'), HA),    # early FT due: held (1 min)
                _2h('90', ('1', '1'), HA)])   # 2 min up: posts with the old photo
    assert len(world.asked) == 1
    assert rec['posts'] == ['FT']
    assert world.edits and 'No answer in 2 minutes' in world.edits[0][1]


def test_a_correction_never_deletes_the_live_card_while_held(loop, world):
    rec = loop([_2h('90', ('1', '0'), H),     # photo noted, early FT posted (1-0)
                _2h('92', ('1', '1'), HA),    # level: asked; correction held
                _2h('92', ('1', '1'), HA)])   # still inside 2 minutes
    assert rec['posts'] == ['FT']
    assert rec['deleted'] == []               # the live card stays up meanwhile
    assert len(world.asked) == 1


# ── The bot's side ───────────────────────────────────────────────────────────

def test_post_the_older_one_is_recorded(world):
    qid = _asked(world)
    text, state = sp.record_choice(sp.callback(MATCH_ID, 'FT', qid, 'old'))
    assert state['answer'] == 'old' and 'older photo' in text


def test_a_tap_after_the_card_went_is_too_late(world):
    qid = _asked(world)
    world.remote['FT']['closed'] = True
    text, state = sp.record_choice(sp.callback(MATCH_ID, 'FT', qid, 'new'))
    assert state is None and text.startswith('Too late')


def test_upload_new_then_a_photo_replaces_the_background(world, monkeypatch):
    import telegram_bot as tb
    qid = _asked(world)
    uploads = []

    async def fake_upload(msg, context, match, moment, ref, **kw):
        uploads.append((match['match_id'], moment, kw.get('public_id')))
        return True

    monkeypatch.setattr(tb, '_upload_photo', fake_upload)
    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(tb, '_touch_session', lambda: None)
    monkeypatch.setattr(tb, '_photo_ref', lambda m: ('f', 1))
    monkeypatch.setattr(tb, '_load_matches', lambda: [])
    monkeypatch.setattr(tb.time, 'time', lambda: world.t)

    class Q:
        data = sp.callback(MATCH_ID, 'FT', qid, 'new')
        message = None

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **k):
            self.text = text

    ctx = types.SimpleNamespace(user_data={}, chat_data={})
    q = Q()
    assert asyncio.run(tb.story_new_tap(types.SimpleNamespace(callback_query=q), ctx)) \
        == tb.STORY_WAIT
    assert 'Send the new photo now' in q.text

    class Msg:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **k):
            self.replies.append(text)

    msg = Msg()
    state = asyncio.run(tb.story_photo_received(types.SimpleNamespace(message=msg), ctx))
    assert state == tb.ConversationHandler.END
    assert uploads == [(MATCH_ID, 'FT', None)]       # the FT background slot itself
    assert world.remote['FT']['uploaded'] is True
    assert 'New photo in' in msg.replies[-1]


def test_the_bot_wires_both_buttons():
    import telegram_bot as tb
    src = open(tb.__file__, encoding='utf-8').read()
    assert r"pattern=r'^st:[^:]+:(HT|FT):\d+:new$'" in src
    assert r"pattern=r'^st:[^:]+:(HT|FT):\d+:old$'" in src
    assert src.index('app.add_handler(story_conv)') < src.index('app.add_handler(conv)')
