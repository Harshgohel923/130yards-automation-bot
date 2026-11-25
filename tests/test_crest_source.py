"""
Every crest comes from football-logos.cc via logo_fetch — no hard-coded links.

config.py used to carry a hand-mapped table of World Cup national-team crests
pointing at one-off Cloudinary uploads. It is gone: national teams resolve to
assets/national/<name> exactly as clubs resolve to assets/club/<name>.
"""

import types

import config
import logo_fetch


def test_there_is_no_hand_mapped_crest_table():
    assert not hasattr(config, 'CLOUDINARY_TEAM_CRESTS')
    src = open(config.__file__, encoding='utf-8').read()
    assert 'national-team_3000x3000' not in src


def _resolve(monkeypatch, team, existing):
    seen = []

    def head(url, timeout=None):
        seen.append(url)
        return types.SimpleNamespace(
            status_code=200 if any(url.endswith(f'{p}.png') for p in existing)
            else 404)

    import requests
    monkeypatch.setattr(requests, 'head', head)
    config._CREST_URL_CACHE.clear()
    return config.get_crest_url(team, alert=False), seen


def test_a_former_world_cup_team_uses_the_standard_national_path(monkeypatch):
    url, seen = _resolve(monkeypatch, 'Mexico', {'assets/national/mexico'})
    assert url.endswith('/assets/national/mexico.png')
    assert 'football-logos.cc_' not in url


def test_a_club_still_resolves_the_same_way(monkeypatch):
    url, _ = _resolve(monkeypatch, 'Arsenal', {'assets/club/arsenal'})
    assert url.endswith('/assets/club/arsenal.png')


def test_a_missing_crest_is_reported_missing_not_hard_coded(monkeypatch):
    url, _ = _resolve(monkeypatch, 'Mexico', set())
    assert url is None


# ── The image host's rate limit ──────────────────────────────────────────────

class _Resp:
    def __init__(self, status, text='', ctype='image/png'):
        self.status_code = status
        self.text = text
        self.headers = {'content-type': ctype}
        self.content = b'png'

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


def test_a_try_again_404_is_retried_after_a_wait(monkeypatch):
    replies = iter([_Resp(404, 'Please try again after a moment.'),
                    _Resp(404, 'Please try again after a moment.'),
                    _Resp(200)])
    waits = []
    monkeypatch.setattr(logo_fetch.requests, 'get', lambda *a, **k: next(replies))
    monkeypatch.setattr(logo_fetch.time, 'sleep', waits.append)
    assert logo_fetch._get_image('https://x/y.png').status_code == 200
    assert waits == list(logo_fetch.RATE_LIMIT_WAITS[:2])


def test_a_real_404_fails_at_once(monkeypatch):
    waits = []
    monkeypatch.setattr(logo_fetch.requests, 'get',
                        lambda *a, **k: _Resp(404, 'Not Found'))
    monkeypatch.setattr(logo_fetch.time, 'sleep', waits.append)
    try:
        logo_fetch._get_image('https://x/y.png')
    except RuntimeError:
        pass
    else:
        raise AssertionError('expected the 404 to raise')
    assert waits == []


def test_it_gives_up_after_the_last_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(logo_fetch.requests, 'get',
                        lambda *a, **k: _Resp(404, 'Please try again after a moment.'))
    monkeypatch.setattr(logo_fetch.time, 'sleep', waits.append)
    try:
        logo_fetch._get_image('https://x/y.png')
    except RuntimeError:
        pass
    assert waits == list(logo_fetch.RATE_LIMIT_WAITS)


def test_a_bare_429_is_retried_too(monkeypatch):
    replies = iter([_Resp(429, ''), _Resp(200)])
    waits = []
    monkeypatch.setattr(logo_fetch.requests, 'get', lambda *a, **k: next(replies))
    monkeypatch.setattr(logo_fetch.time, 'sleep', waits.append)
    assert logo_fetch._get_image('https://x/y.png').status_code == 200
    assert waits == [logo_fetch.RATE_LIMIT_WAITS[0]]


# ── Competition logos: the World Cup too ─────────────────────────────────────

def test_there_is_no_hand_mapped_tournament_logo():
    assert not hasattr(config, 'CLOUDINARY_TOURNAMENT_LOGO')
    assert 'football-logos.cc_' not in open(config.__file__, encoding='utf-8').read()


def test_the_world_cup_resolves_to_the_football_logos_asset(monkeypatch):
    import requests
    seen = []

    def head(url, timeout=None):
        seen.append(url)
        return types.SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, 'head', head)
    config._COMP_URL_CACHE.clear()
    for name in ('World Cup', 'FIFA World Cup 2026'):
        url = config.get_competition_logo_url(name, alert=False)
        assert url.endswith('/assets/competition/fifa-world-cup.png'), url
    assert logo_fetch.COMPETITIONS['fifa-world-cup'] == (
        'tournaments', 'fifa-world-cup-2026', 'white')


def test_the_photo_card_still_trims_the_wordmark_off_the_new_path():
    src = open(__import__('overlay_scorebar').__file__, encoding='utf-8').read()
    assert "'/competition/fifa-world-cup.' in url" in src
