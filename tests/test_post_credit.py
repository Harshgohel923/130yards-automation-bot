"""
Credit for bot posts: who contributed to each post, and how it did.

Posting records the contributors (read off the photos' own metadata) at
stats/posts/<week>/<media id>.json; the Monday recap asks Instagram how each
post did and adds it up per person. Likes and comments always; reach, saves
and shares only if the token has the insights permission.
"""

import json
import types

import pytest

import post_credit as pc


class FakeCloudinary:
    """Enough of the Cloudinary SDK for post_credit."""

    def __init__(self, assets=None):
        self.assets = assets or {}          # public_id → context.custom
        self.raw = {}                       # public_id → JSON written
        outer = self

        class Api:
            @staticmethod
            def resource(public_id, **k):
                if public_id not in outer.assets:
                    raise Exception('Resource not found')
                return {'context': {'custom': outer.assets[public_id]}}

            @staticmethod
            def resources(prefix='', **k):
                return {'resources': [{'public_id': pid, 'secure_url': f'raw://{pid}'}
                                      for pid in outer.raw if pid.startswith(prefix)]}

        class Uploader:
            @staticmethod
            def upload(blob, public_id=None, **k):
                outer.raw[public_id] = json.loads(blob.read())
                return {'public_id': public_id}

        self.api, self.uploader = Api, Uploader


@pytest.fixture
def cloud(monkeypatch):
    fake = FakeCloudinary()
    monkeypatch.setattr(pc, '_cloudinary', lambda: fake)
    monkeypatch.setattr('cloudinary_utils.list_extra_photos', lambda mid, ev: [
        {'public_id': pid} for pid in sorted(fake.assets) if pid.startswith(f'match_photos/{mid}_{ev}_extra_')])
    return fake


HARSH = {'uploaded_by': 'Harsh', 'uploaded_by_id': '1', 'uploaded_by_username': 'Gohellllll'}
YASH = {'uploaded_by': 'Yash', 'uploaded_by_id': '3', 'uploaded_by_username': 'Yashulllaaa'}


def test_uploader_comes_off_the_photo(cloud):
    cloud.assets['match_photos/42_FT'] = HARSH
    assert pc.uploader_of('match_photos/42_FT') == {'id': '1', 'name': 'Harsh',
                                                    'username': 'Gohellllll'}
    assert pc.uploader_of('match_photos/missing') is None


def test_an_older_photo_is_credited_by_name(cloud):
    cloud.assets['match_photos/42_HT'] = {'uploaded_by': 'Kishan'}
    person = pc.uploader_of('match_photos/42_HT')
    assert pc.person_key(person) == 'name:Kishan'


def test_card_contributors_include_extras_once_each(cloud):
    cloud.assets.update({'match_photos/42_FT': HARSH,
                         'match_photos/42_FT_extra_01': YASH,
                         'match_photos/42_FT_extra_02': HARSH})
    people = pc.contributors_for_card('42', 'FT')
    assert [p['name'] for p in people] == ['Harsh', 'Yash']


def test_ht_cards_have_no_extras(cloud):
    cloud.assets.update({'match_photos/42_HT': HARSH, 'match_photos/42_HT_extra_01': YASH})
    assert [p['name'] for p in pc.contributors_for_card('42', 'HT')] == ['Harsh']


def test_a_post_is_filed_under_its_week(cloud):
    ts = 1795644000          # 2026-11-25 22:00 UTC → 2026-W48
    assert pc.record_post('m1', 'FT card', 'A vs B',
                          [{'id': '1', 'name': 'Harsh', 'username': 'G'}], ts)
    (pid, record), = cloud.raw.items()
    assert pid == 'stats/posts/2026-W48/m1.json'
    assert record['contributors'][0]['name'] == 'Harsh'


def test_nothing_is_filed_without_contributors(cloud):
    assert not pc.record_post('m1', 'FT card', 'A vs B', [None])
    assert cloud.raw == {}


def test_recording_never_raises(monkeypatch):
    monkeypatch.setattr(pc, '_cloudinary', lambda: (_ for _ in ()).throw(RuntimeError('down')))
    assert pc.record_post('m1', 'k', 'l', [{'id': '1', 'name': 'A'}]) is False


# ── How the posts did ────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


@pytest.fixture
def instagram(monkeypatch, cloud):
    w = types.SimpleNamespace(media={}, insights=None, insight_calls=0)

    def get(url, params=None, timeout=None):
        if url.startswith('raw://'):
            return Resp(200, cloud.raw[url[len('raw://'):]])
        media_id = url.rsplit('/', 2)[-2] if url.endswith('/insights') else url.rsplit('/', 1)[-1]
        if url.endswith('/insights'):
            w.insight_calls += 1
            if w.insights is None:
                return Resp(400, {'error': {'message': 'Application does not have permission'}})
            return Resp(200, {'data': [{'name': k, 'values': [{'value': v}]}
                                       for k, v in w.insights.items()]})
        if media_id not in w.media:
            return Resp(400, {'error': {'message': 'does not exist'}})
        return Resp(200, w.media[media_id])

    monkeypatch.setattr(pc.requests, 'get', get)
    return w


def _file(cloud, media_id, *people, week='2026-W48'):
    cloud.raw[f'stats/posts/{week}/{media_id}.json'] = {
        'media_id': media_id, 'contributors': list(people)}


H = {'id': '1', 'name': 'Harsh', 'username': 'Gohellllll'}
Y = {'id': '3', 'name': 'Yash', 'username': 'Yashulllaaa'}


def test_likes_and_comments_add_up_per_person(cloud, instagram):
    _file(cloud, 'm1', H)
    _file(cloud, 'm2', H, Y)
    instagram.media = {'m1': {'like_count': 10, 'comments_count': 1},
                       'm2': {'like_count': 30, 'comments_count': 2}}
    credit = pc.weekly_credit('2026-W48')
    assert credit['1'] == {'name': 'Harsh', 'username': 'Gohellllll', 'posts': 2,
                           'likes': 40, 'comments': 3}
    assert credit['3']['posts'] == 1 and credit['3']['likes'] == 30


def test_without_the_permission_insights_are_asked_once(cloud, instagram):
    for n in range(4):
        _file(cloud, f'm{n}', H)
        instagram.media[f'm{n}'] = {'like_count': 1, 'comments_count': 0}
    credit = pc.weekly_credit('2026-W48')
    assert instagram.insight_calls == 1
    assert 'reach' not in credit['1']


def test_with_the_permission_reach_and_saves_come_too(cloud, instagram):
    _file(cloud, 'm1', H)
    instagram.media = {'m1': {'like_count': 5, 'comments_count': 0}}
    instagram.insights = {'reach': 900, 'saved': 12, 'shares': 3}
    assert pc.weekly_credit('2026-W48')['1'] == {
        'name': 'Harsh', 'username': 'Gohellllll', 'posts': 1, 'likes': 5,
        'comments': 0, 'reach': 900, 'saved': 12, 'shares': 3}


def test_a_deleted_post_is_skipped(cloud, instagram):
    _file(cloud, 'gone', H)                      # a corrected early card, deleted
    _file(cloud, 'm1', H)
    instagram.media = {'m1': {'like_count': 7, 'comments_count': 0}}
    assert pc.weekly_credit('2026-W48')['1']['posts'] == 1


def test_other_weeks_are_not_counted(cloud, instagram):
    _file(cloud, 'm1', H, week='2026-W47')
    assert pc.weekly_credit('2026-W48') == {}


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_posting_a_card_records_its_credit(monkeypatch, tmp_path):
    import main
    import matchlog
    recorded = []
    p = tmp_path / 'card.png'
    p.write_bytes(b'x')
    monkeypatch.setattr(main, '_generate_slides', lambda *a: [str(p)])
    monkeypatch.setattr(main, 'upload_image', lambda path: ('https://cdn/c.png', 'gen/c'))
    monkeypatch.setattr(main, 'list_extra_photos', lambda *a: [])
    monkeypatch.setattr(main, 'generate_caption', lambda *a, **k: 'cap')
    monkeypatch.setattr(main, 'post_to_instagram', lambda url, cap: 'ig-1')
    monkeypatch.setattr(matchlog, 'log', lambda *a, **k: None)
    monkeypatch.setattr(pc, 'contributors_for_card', lambda mid, ev: [H])
    monkeypatch.setattr(pc, 'record_post', lambda *a, **k: recorded.append(a))
    main._post_slides({'match_id': '42', 'home_team': 'A', 'away_team': 'B'}, 'FT',
                      {'matchSample': {}})
    assert recorded == [('ig-1', 'FT card', 'A vs B', [H])]


def test_uploads_carry_the_senders_id_and_username(monkeypatch):
    import asyncio
    import telegram_bot as tb
    seen = {}

    class TgFile:
        async def download_to_drive(self, path):
            open(path, 'wb').write(b'x')

    class Bot:
        async def get_file(self, file_id):
            return TgFile()

    class Msg:
        async def reply_text(self, *a, **k):
            pass

    monkeypatch.setattr(tb.cloudinary.uploader, 'upload',
                        lambda path, **kw: seen.update(kw) or {'public_id': kw['public_id'],
                                                               'secure_url': 'u'})
    monkeypatch.setattr(tb, '_shrink_for_upload', lambda p: False)
    ctx = types.SimpleNamespace(user_data={}, bot=Bot(), chat_data={
        'last_user': 'Harsh', 'last_user_id': 1329280363, 'last_username': 'Gohellllll'})
    asyncio.run(tb._upload_photo(Msg(), ctx, {'match_id': '42', 'home_team': 'A',
                                              'away_team': 'B'}, 'FT', ('f', 1)))
    assert seen['context'] == {'uploaded_by': 'Harsh', 'uploaded_by_id': '1329280363',
                               'uploaded_by_username': 'Gohellllll'}


def test_every_posting_path_records_credit():
    import inspect
    import carousel
    import main
    import telegram_bot as tb
    assert 'post_credit.record_post' in inspect.getsource(main._post_slides)
    assert 'post_credit.record_post' in inspect.getsource(main._post_event_photo)
    assert 'post_credit.record_post' in inspect.getsource(carousel.publish_group)
    assert 'post_credit.record_post' in inspect.getsource(tb._publish)
