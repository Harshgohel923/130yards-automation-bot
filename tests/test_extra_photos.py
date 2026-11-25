"""
Extra photos as carousel slides in an HT/FT post.

Sent through /start after the background; stored as
match_photos/<id>_<HT|FT>_extra_NN. When the worker posts, the slides are:
scorecard, the extras in the order sent, the stats page last. Extras are
cropped to the card's 3:4 at delivery and never deleted by a correction.
"""

import asyncio
import types

import pytest

import cloudinary_utils as cu
import main
import matchlog
import telegram_bot as tb


MATCH = {'match_id': '42', 'home_team': 'Arsenal', 'away_team': 'Chelsea'}


def run(coro):
    return asyncio.run(coro)


# ── Storage ──────────────────────────────────────────────────────────────────

def test_extra_ids_sort_in_the_order_sent():
    assert cu.extra_photo_public_id('42', 'ht', 3) == 'match_photos/42_HT_extra_03'
    ids = [cu.extra_photo_public_id('42', 'FT', n) for n in (10, 2, 1)]
    assert sorted(ids) == ['match_photos/42_FT_extra_01', 'match_photos/42_FT_extra_02',
                           'match_photos/42_FT_extra_10']


def test_a_removed_middle_photo_never_gets_overwritten():
    existing = [{'public_id': 'match_photos/42_HT_extra_01'},
                {'public_id': 'match_photos/42_HT_extra_03'}]
    assert cu.next_extra_photo_number(existing) == 4
    assert cu.next_extra_photo_number([]) == 1


def test_listing_keeps_only_this_posts_extras_in_order(monkeypatch):
    monkeypatch.setattr(cu.cloudinary.api, 'resources', lambda **k: {'resources': [
        {'public_id': 'match_photos/42_HT_extra_02', 'secure_url': 'u2'},
        {'public_id': 'match_photos/42_HT_extra_01', 'secure_url': 'u1'},
    ]})
    assert [x['url'] for x in cu.list_extra_photos('42', 'HT')] == ['u1', 'u2']


def test_the_slide_url_crops_to_three_by_four_as_jpeg():
    url = 'https://res.cloudinary.com/demo/image/upload/v7/match_photos/42_HT_extra_01.jpg'
    assert cu.extra_slide_url(url) == (
        'https://res.cloudinary.com/demo/image/upload/'
        'c_fill,ar_3:4,g_auto,w_1440,q_auto,f_jpg/v7/match_photos/42_HT_extra_01.jpg')


# ── The worker's post ────────────────────────────────────────────────────────

@pytest.fixture
def post(monkeypatch, tmp_path):
    rec = {'alerts': [], 'carousel': None, 'single': None}
    for name in ('log', 'warn'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)

    def slides(entry, event_type, data):
        paths = []
        for name in (['card'] + (['stats'] if rec.get('stats') else [])):
            p = tmp_path / f'{name}.png'
            p.write_bytes(b'x')
            paths.append(str(p))
        return paths

    monkeypatch.setattr(main, '_generate_slides', slides)
    monkeypatch.setattr(main, 'upload_image',
                        lambda p: (f"https://cdn/{p.rsplit('/', 1)[-1]}", f"gen/{p.rsplit('/', 1)[-1]}"))
    monkeypatch.setattr(main, 'generate_caption', lambda *a, **k: 'cap')
    monkeypatch.setattr(main, 'send_alert', lambda t, **k: rec['alerts'].append(t))
    monkeypatch.setattr(main, 'post_carousel_to_instagram',
                        lambda urls, cap: rec.__setitem__('carousel', urls) or 'ig-c')
    monkeypatch.setattr(main, 'post_to_instagram',
                        lambda url, cap: rec.__setitem__('single', url) or 'ig-s')

    def go(extras, stats=False, event_type='FT', fail=False):
        rec['stats'] = stats

        def listing(mid, ev):
            if fail:
                raise RuntimeError('cloudinary down')
            return [{'public_id': f'match_photos/42_{ev}_extra_{i:02d}',
                     'url': f'https://res.cloudinary.com/d/image/upload/x{i}.jpg'}
                    for i in range(1, extras + 1)]
        monkeypatch.setattr(main, 'list_extra_photos', listing)
        cids, ig = main._post_slides(dict(MATCH), event_type, {'matchSample': {}})
        rec['cids'] = cids
        return rec
    return go


def test_stats_page_goes_last_after_the_extras(post):
    rec = post(extras=2, stats=True)
    urls = rec['carousel']
    assert urls[0] == 'https://cdn/card.png'
    assert all('c_fill,ar_3:4' in u for u in urls[1:3])
    assert urls[-1] == 'https://cdn/stats.png'
    assert len(urls) == 4


def test_extras_are_never_in_what_a_correction_deletes(post):
    rec = post(extras=3, stats=True)
    assert rec['cids'] == ['gen/card.png', 'gen/stats.png']


def test_ht_stays_a_single_card_even_if_extras_exist(post):
    rec = post(extras=2, event_type='HT')
    assert rec['single'] == 'https://cdn/card.png' and rec['carousel'] is None


def test_no_extras_is_unchanged(post):
    rec = post(extras=0)
    assert rec['single'] == 'https://cdn/card.png' and rec['carousel'] is None


def test_too_many_extras_are_trimmed_to_fit_ten_slides(post):
    rec = post(extras=9, stats=True)
    assert len(rec['carousel']) == 10
    assert rec['carousel'][-1] == 'https://cdn/stats.png'
    assert any('only 8 fit' in a for a in rec['alerts'])


def test_a_listing_failure_never_costs_the_post(post):
    rec = post(extras=2, fail=True)
    assert rec['single'] == 'https://cdn/card.png'


# ── The bot ──────────────────────────────────────────────────────────────────

class Msg:
    chat_id = 1

    def __init__(self):
        self.replies = []
        self.photo, self.document = [], None

    async def reply_text(self, text, reply_markup=None, **k):
        self.replies.append((text, [b.callback_data for row in reply_markup.inline_keyboard
                                    for b in row] if reply_markup else []))


class Query:
    def __init__(self, data):
        self.data = data
        self.message = Msg()
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **k):
        self.edits.append(text)

    async def edit_message_caption(self, caption=None, **k):
        self.edits.append(caption)


@pytest.fixture
def bot(monkeypatch):
    w = types.SimpleNamespace(extras=[], uploads=[], deleted=[])
    monkeypatch.setattr(tb, 'list_extra_photos', lambda mid, ev: list(w.extras))

    def delete(mid, ev):
        n = len(w.extras)
        w.deleted.append((mid, ev))
        w.extras.clear()
        return n
    monkeypatch.setattr(tb, 'delete_extra_photos', delete)

    async def upload(msg, context, match, event_type, ref, **kw):
        w.uploads.append(kw.get('public_id'))
        w.extras.append({'public_id': kw.get('public_id'), 'url': 'u'})
        return True
    monkeypatch.setattr(tb, '_upload_photo', upload)
    monkeypatch.setattr(tb, '_photo_ref', lambda m: ('file', 1))
    w.ctx = types.SimpleNamespace(user_data={'match': dict(MATCH), 'event_type': 'FT'},
                                  chat_data={}, bot=None)
    return w


def test_the_menu_offers_add_and_done(bot):
    msg = Msg()
    assert run(tb._extra_menu(msg, bot.ctx)) == tb.EXTRA_MENU
    text, buttons = msg.replies[-1]
    assert buttons == ['xp:add', 'xp:done']
    assert '0 extra photos so far' in text


def test_the_menu_says_stats_go_last_for_an_ft_stats_post(bot):
    bot.ctx.user_data.update(event_type='FT', match=dict(MATCH, post_ft_stats=True))
    msg = Msg()
    run(tb._extra_menu(msg, bot.ctx))
    assert 'before the stats page' in msg.replies[-1][0]


def test_half_time_gets_no_extras(bot):
    bot.ctx.user_data['event_type'] = 'HT'
    msg = Msg()
    assert run(tb._extra_menu(msg, bot.ctx)) == tb.ConversationHandler.END
    assert msg.replies == []


def test_a_grouped_matchs_ft_gets_no_extras(bot):
    bot.ctx.user_data.update(event_type='FT', match=dict(MATCH, carousel_group='sat'))
    msg = Msg()
    assert run(tb._extra_menu(msg, bot.ctx)) == tb.ConversationHandler.END
    assert msg.replies == []


def test_adding_photos_numbers_them_in_order(bot):
    q = Query('xp:add')
    assert run(tb.extra_choice(types.SimpleNamespace(callback_query=q), bot.ctx)) == tb.EXTRA_WAIT
    for _ in range(2):
        state = run(tb.extra_photo_received(types.SimpleNamespace(message=Msg()), bot.ctx))
        assert state == tb.EXTRA_MENU
    assert bot.uploads == ['match_photos/42_FT_extra_01', 'match_photos/42_FT_extra_02']


def test_the_limit_is_enforced(bot):
    bot.extras = [{'public_id': f'match_photos/42_FT_extra_{i:02d}', 'url': 'u'}
                  for i in range(1, tb.EXTRA_PHOTO_LIMIT + 1)]
    msg = Msg()
    run(tb.extra_photo_received(types.SimpleNamespace(message=msg), bot.ctx))
    assert bot.uploads == []
    assert "wasn't added" in msg.replies[0][0]
    assert 'xp:add' not in msg.replies[-1][1]      # menu no longer offers more


def test_remove_clears_them_and_shows_the_menu_again(bot):
    bot.extras = [{'public_id': 'match_photos/42_FT_extra_01', 'url': 'u'}]
    q = Query('xp:clear')
    assert run(tb.extra_choice(types.SimpleNamespace(callback_query=q), bot.ctx)) == tb.EXTRA_MENU
    assert bot.deleted == [('42', 'FT')]
    assert 'Removed 1 extra photo' in q.edits[0]


def test_done_ends_and_says_how_many(bot):
    bot.extras = [{'public_id': 'a', 'url': 'u'}, {'public_id': 'b', 'url': 'u'}]
    q = Query('xp:done')
    assert run(tb.extra_choice(types.SimpleNamespace(callback_query=q), bot.ctx)) \
        == tb.ConversationHandler.END
    assert '2 extra photos will go up' in q.edits[0]


def test_keep_and_add_from_the_overwrite_warning(bot):
    bot.ctx.user_data['overwrite'] = {'kind': 'scorecard'}
    bot.ctx.user_data['pending'] = ('held', 1)
    q = Query('ow:extras')
    assert run(tb.overwrite_choice(types.SimpleNamespace(callback_query=q), bot.ctx)) \
        == tb.EXTRA_MENU
    assert "wasn't used as the background" in q.edits[0]
    assert bot.uploads == []


def test_the_start_conversation_has_the_extra_states():
    src = open(tb.__file__, encoding='utf-8').read()
    assert "EXTRA_MENU: [CallbackQueryHandler(extra_choice" in src
    assert "MessageHandler(PHOTO_ENTRY_FILTER, extra_photo_received)" in src
