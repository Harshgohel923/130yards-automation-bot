"""
"A photo is already uploaded for this — replace it?"

Once a photo slot is known (a match's HT/FT background, a staged event
picture, the HT card asked for on request) and before a new photo is asked
for or uploaded, the bot checks it. If something is there it shows it — with
who sent it and when — and asks: Replace it / Keep it.
"""

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

import telegram_bot as tb


MATCH = {'match_id': '42', 'home_team': 'Arsenal', 'away_team': 'Chelsea'}
EXISTING = {'url': 'https://res.cloudinary.com/demo/image/upload/v1/match_photos/42_HT',
            'by': 'Harsh', 'ago': '12 minutes ago'}


def run(coro):
    return asyncio.run(coro)


class Msg:
    chat_id = 1

    def __init__(self, text=''):
        self.text = text
        self.replies = []
        self.photo, self.document = [], None

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get('reply_markup')))


class Query:
    def __init__(self, data, text=''):
        self.data = data
        self.message = Msg(text)
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **k):
        self.edits.append(text)

    async def edit_message_caption(self, caption=None, reply_markup=None, **k):
        self.edits.append(caption)


class Bot:
    def __init__(self, fail=False):
        self.photos, self.fail = [], fail

    async def send_photo(self, chat_id=None, photo=None, caption=None, reply_markup=None):
        if self.fail:
            raise RuntimeError('wrong file identifier/HTTP URL specified')
        self.photos.append({'photo': photo, 'caption': caption,
                            'buttons': [b.callback_data for row in reply_markup.inline_keyboard
                                        for b in row]})


@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(existing=None, uploads=[], recorded=[])

    async def existing(public_id):
        w.looked_up = public_id
        return w.existing

    async def upload(msg, context, match, event_type, ref, **kw):
        w.uploads.append((match['match_id'], event_type, ref, kw.get('public_id')))
        return True

    async def staged_menu(msg, context):
        return 'STAGED_MENU'

    monkeypatch.setattr(tb, '_existing_photo', existing)
    monkeypatch.setattr(tb, '_upload_photo', upload)
    monkeypatch.setattr(tb, '_staged_menu', staged_menu)
    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(tb, '_touch_session', lambda: None)
    w.bot = Bot()
    w.ctx = types.SimpleNamespace(user_data={}, chat_data={}, bot=w.bot)
    return w


# ── /start: HT/FT backgrounds ────────────────────────────────────────────────

def _choose_event(w, event='HT'):
    w.ctx.user_data['match'] = dict(MATCH)
    q = Query(f'event:{event}')
    return run(tb.event_chosen(types.SimpleNamespace(callback_query=q), w.ctx)), q


def test_an_empty_slot_just_asks_for_the_photo(world):
    state, q = _choose_event(world)
    assert state == tb.WAIT_PHOTO
    assert world.bot.photos == []
    assert world.looked_up == 'match_photos/42_HT'


def test_a_filled_slot_shows_the_photo_and_asks(world):
    world.existing = EXISTING
    state, q = _choose_event(world)
    assert state == tb.OW_CONFIRM
    (shown,) = world.bot.photos
    assert '/image/upload/w_1080,q_auto,f_jpg/' in shown['photo']       # phone-sized
    assert 'already uploaded for Arsenal vs Chelsea — HT' in shown['caption']
    assert 'by Harsh and 12 minutes ago' in shown['caption']
    assert shown['buttons'] == ['ow:replace', 'ow:keep']
    assert world.uploads == []


def test_replace_uploads_the_photo_already_held(world):
    world.existing = EXISTING
    world.ctx.user_data['pending'] = ('file-id', 100)       # photo sent first
    _choose_event(world)
    q = Query('ow:replace')
    state = run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx))
    assert state == tb.ConversationHandler.END
    assert world.uploads == [('42', 'HT', ('file-id', 100), None)]
    assert q.edits == ['🔁 Replacing it.']


def test_replace_without_a_photo_asks_for_one(world):
    world.existing = EXISTING
    _choose_event(world)
    q = Query('ow:replace')
    state = run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx))
    assert state == tb.WAIT_PHOTO
    assert 'Now send the photo' in q.message.replies[-1][0]


def test_keep_uploads_nothing_and_says_the_new_photo_was_not_used(world):
    world.existing = EXISTING
    world.ctx.user_data['pending'] = ('file-id', 100)
    _choose_event(world)
    q = Query('ow:keep')
    state = run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx))
    assert state == tb.ConversationHandler.END
    assert world.uploads == []
    assert "wasn't used" in q.edits[0]
    assert 'pending' not in world.ctx.user_data


def test_if_the_preview_cannot_be_sent_the_question_still_is(world):
    world.existing = EXISTING
    world.bot.fail = True
    w = world
    w.ctx.user_data['match'] = dict(MATCH)
    q = Query('event:FT')
    state = run(tb.event_chosen(types.SimpleNamespace(callback_query=q), w.ctx))
    assert state == tb.OW_CONFIRM
    text, markup = q.message.replies[-1]
    assert 'already uploaded' in text and EXISTING['url'] in text
    assert markup is not None


# ── /event: staged pictures ──────────────────────────────────────────────────

def _settle(w, pending=None):
    w.ctx.user_data.update({'match': dict(MATCH), 'event_key': 'GOAL'})
    if pending:
        w.ctx.user_data['pending'] = pending
    return run(tb._player_settled(Msg(), w.ctx, 'Saka'))


def test_event_slot_filled_warns_with_the_player_and_moment(world):
    world.existing = EXISTING
    assert _settle(world) == tb.OW_CONFIRM
    caption = world.bot.photos[0]['caption']
    assert 'Saka' in caption and 'Arsenal vs Chelsea' in caption
    assert world.looked_up.startswith('event_photos/42_GOAL_')


def test_event_keep_returns_to_the_staged_menu(world):
    world.existing = EXISTING
    _settle(world, pending=('f', 1))
    q = Query('ow:keep')
    assert run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx)) \
        == 'STAGED_MENU'
    assert world.uploads == []


def test_event_replace_stages_the_held_photo(world):
    world.existing = EXISTING
    _settle(world, pending=('f', 1))
    q = Query('ow:replace')
    assert run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx)) \
        == 'STAGED_MENU'
    (match_id, key, ref, public_id), = world.uploads
    assert key == 'GOAL' and ref == ('f', 1) and public_id.startswith('event_photos/42_GOAL_')


def test_event_slot_empty_goes_straight_on(world):
    assert _settle(world) == tb.E_PHOTO
    assert world.bot.photos == []


# ── The HT card asked for on request ─────────────────────────────────────────

def _ht_post(w, monkeypatch):
    import ht_optin
    monkeypatch.setattr(ht_optin, 'write_answer',
                        lambda mid, ans, style=None: w.recorded.append((mid, ans, style)))
    q = Query('ht:42:post', text='🙋 Arsenal vs Chelsea — half time, 2-0.\n\nMore')
    return run(tb.ht_post_tap(types.SimpleNamespace(callback_query=q), w.ctx)), q


def test_ht_with_a_photo_already_offers_use_replace_or_template(world, monkeypatch):
    world.existing = EXISTING
    state, q = _ht_post(world, monkeypatch)
    assert state == tb.OW_CONFIRM
    assert world.bot.photos[0]['buttons'] == ['ow:keep', 'ow:replace', 'htbg:42']
    assert "already a half-time photo" in q.edits[0]
    assert world.recorded == []


def test_ht_use_this_photo_records_the_go_ahead(world, monkeypatch):
    world.existing = EXISTING
    _ht_post(world, monkeypatch)
    q = Query('ow:keep')
    state = run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx))
    assert state == tb.ConversationHandler.END
    assert world.recorded == [('42', 'post', 'photo')]
    assert 'with your photo' in q.edits[0]


def test_ht_replace_asks_for_the_new_photo(world, monkeypatch):
    world.existing = EXISTING
    _ht_post(world, monkeypatch)
    q = Query('ow:replace')
    state = run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), world.ctx))
    assert state == tb.HT_BG
    assert world.recorded == []
    text, markup = q.message.replies[-1]
    assert 'Send the background photo' in text
    assert markup.inline_keyboard[0][0].callback_data == 'htbg:42'


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_uploads_record_who_sent_them(monkeypatch):
    calls = {}

    class TgFile:
        async def download_to_drive(self, path):
            open(path, 'wb').write(b'x')

    class B:
        async def get_file(self, file_id):
            return TgFile()

    def upload(path, **kw):
        calls.update(kw)
        return {'public_id': kw['public_id'], 'secure_url': 'u'}

    monkeypatch.setattr(tb.cloudinary.uploader, 'upload', upload)
    monkeypatch.setattr(tb, '_shrink_for_upload', lambda p: False)
    ctx = types.SimpleNamespace(user_data={}, chat_data={'last_user': 'Ha|rsh='}, bot=B())
    assert run(tb._upload_photo(Msg(), ctx, MATCH, 'HT', ('f', 10)))
    assert calls['context'] == {'uploaded_by': 'Ha rsh'}


def test_existing_photo_reads_the_admin_api(monkeypatch):
    when = (datetime.now(timezone.utc) - timedelta(minutes=12)).strftime('%Y-%m-%dT%H:%M:%SZ')
    monkeypatch.setattr(tb.cloudinary.api, 'resource', lambda pid, **k: {
        'secure_url': 'https://x/v9/a.jpg', 'created_at': when,
        'context': {'custom': {'uploaded_by': 'Harsh'}}})
    assert run(tb._existing_photo('match_photos/42_HT')) == {
        'url': 'https://x/v9/a.jpg', 'by': 'Harsh', 'ago': '12 minutes ago'}


def test_a_missing_or_unreadable_slot_counts_as_empty(monkeypatch):
    def missing(pid, **k):
        raise Exception('Resource not found - match_photos/42_HT')
    monkeypatch.setattr(tb.cloudinary.api, 'resource', missing)
    assert run(tb._existing_photo('match_photos/42_HT')) is None


@pytest.mark.parametrize('minutes,expected', [
    (0, 'just now'), (12, '12 minutes ago'), (180, '3 hours ago'), (60 * 72, '3 days ago')])
def test_ago(minutes, expected):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    assert tb._ago(when.strftime('%Y-%m-%dT%H:%M:%SZ')) == expected


def test_every_photo_flow_handles_the_question():
    src = open(tb.__file__, encoding='utf-8').read()
    assert src.count("OW_CONFIRM: [CallbackQueryHandler(overwrite_choice") == 2   # /start, /event
    assert "CallbackQueryHandler(overwrite_choice, pattern=r'^ow:(replace|keep)$')," in src  # HT
