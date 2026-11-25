"""
A failed HT or FT card is retried every poll, not written off.

The fallback pipeline (no early card live at the whistle) used to mark the
moment posted whether or not Instagram accepted the card. One transient failure
at half-time meant no half-time card at all, and at full time it also ended the
worker. Now only a card that actually went out closes the moment:

  * HT retries every poll while the scraper reports the interval, and alerts
    once (🙋) if the second half starts with it still unposted;
  * FT retries every poll until it posts or the worker hits its ceiling.

These drive the real worker loop over a scripted run of scrapes, with only the
I/O at its edges replaced.
"""

from datetime import datetime, timedelta, timezone

import pytest

import main
import matchlog
from tests.helpers import goal, scrape


GOAL = [goal(team='A', player='Scorer', minute='23')]


MATCH_ID = '99000001'


class FeedExhausted(Exception):
    """Raised when the scripted scrapes run out, to end the worker loop."""


def _half_time():
    return scrape(minute='45', fs=('1', '0'), hts=('1', '0'),
                  events=GOAL)


def _second_half(minute='50'):
    return scrape(minute=minute, fs=('1', '0'), hts=('1', '0'),
                  events=GOAL)


def _full_time():
    return scrape(status='Played', minute='90', fs=('1', '0'), hts=('1', '0'),
                  events=GOAL)


@pytest.fixture
def worker(monkeypatch):
    """
    Run match_worker over a list of scrapes. `fail` is how many consecutive
    Instagram posts fail before one succeeds.
    """
    rec = {'posts': [], 'alerts': [], 'posted': set()}

    for name in ('start', 'log', 'warn', 'error', 'finish'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)
    monkeypatch.setattr(main.time, 'sleep', lambda s: None)
    monkeypatch.setattr(main, '_save_state', lambda: None)
    monkeypatch.setattr(main, 'get_crest_url', lambda *a, **k: 'crest')
    monkeypatch.setattr(main, 'upsert_match', lambda e: None)
    monkeypatch.setattr(main, 'enrich_with_desktop', lambda *a, **k: None)
    monkeypatch.setattr(main, '_run_event_photos', lambda *a, **k: None)
    monkeypatch.setattr(main, '_clarify_staged_photos', lambda *a, **k: None)
    monkeypatch.setattr(main, '_photo_reminder', lambda *a, **k: None)
    monkeypatch.setattr(main, '_clear_event_photos', lambda *a, **k: None)
    monkeypatch.setattr(main, '_save_match_data', lambda *a, **k: None)
    monkeypatch.setattr(main, '_archive_match_data', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_music_reminder', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_alert',
                        lambda text, **k: rec['alerts'].append(text))
    monkeypatch.setattr(main, 'is_event_posted',
                        lambda mid, et: (mid, et) in rec['posted'])
    monkeypatch.setattr(main, 'mark_event_posted',
                        lambda mid, et: rec['posted'].add((mid, et)))

    def run(scrapes, fail=0):
        feed = iter(scrapes)
        failures = {'left': fail}

        def fake_get(url):
            try:
                return next(feed)
            except StopIteration:
                raise FeedExhausted

        def fake_post(entry, event_type, data):
            rec['posts'].append(event_type)
            if failures['left'] > 0:
                failures['left'] -= 1
                raise RuntimeError('Instagram said no')
            return ['cid'], f'ig-{len(rec["posts"])}'

        monkeypatch.setattr(main, 'get_match_data', fake_get)
        monkeypatch.setattr(main, '_post_slides', fake_post)

        main.MATCH_STATE.clear()
        main.ACTIVE_WORKERS.add(MATCH_ID)
        entry = {
            'match_id': MATCH_ID, 'scraper_url': 'http://x',
            'home_team': 'Home', 'away_team': 'Away',
            'kickoff_utc': (datetime.now(timezone.utc) - timedelta(minutes=50))
                           .isoformat().replace('+00:00', 'Z'),
        }
        try:
            main.match_worker(entry)
            rec['exited'] = True
        except FeedExhausted:
            rec['exited'] = False
        rec['state'] = main.MATCH_STATE[MATCH_ID]
        return rec

    return run


# ── Half-time ────────────────────────────────────────────────────────────────

def test_ht_retries_every_poll_until_it_posts(worker):
    rec = worker([_half_time()] * 4, fail=2)
    assert rec['posts'] == ['HT', 'HT', 'HT']
    assert rec['state']['ht_posted'] is True
    assert (MATCH_ID, 'HT') in rec['posted']


def test_ht_that_posts_first_time_posts_once(worker):
    rec = worker([_half_time()] * 3)
    assert rec['posts'] == ['HT']
    assert not any('half-time' in a for a in rec['alerts'])


def test_ht_still_failing_when_play_resumes_alerts_once_and_stops(worker):
    rec = worker([_half_time()] * 3 + [_second_half()] * 3, fail=99)
    assert rec['posts'] == ['HT', 'HT', 'HT']          # interval only
    assert rec['state']['ht_posted'] is False
    given_up = [a for a in rec['alerts'] if a.startswith('🙋')
                and 'half-time scorecard never made it' in a]
    assert len(given_up) == 1


def test_no_give_up_alert_when_ht_never_failed(worker):
    rec = worker([_half_time(), _second_half(), _second_half()])
    assert rec['posts'] == ['HT']
    assert not any('never made it' in a for a in rec['alerts'])


# ── Full-time ────────────────────────────────────────────────────────────────

def test_ft_failure_does_not_end_the_worker(worker):
    rec = worker([_full_time()] * 5, fail=3)
    assert rec['posts'] == ['FT', 'FT', 'FT', 'FT']
    assert rec['state']['ft_posted'] is True
    assert rec['exited'] is True                       # left via the FT break


def test_ft_keeps_trying_while_it_keeps_failing(worker):
    rec = worker([_full_time()] * 4, fail=99)
    assert rec['posts'] == ['FT'] * 4
    assert rec['state']['ft_posted'] is False
    assert rec['exited'] is False                      # still polling
