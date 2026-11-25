"""
A carousel whose post fails is retried every minute, not every 15.

Only the dispatcher publishes a group (two matches ending together must not
post it twice), and it runs every 15 minutes, so a failed publish used to keep
the whole carousel off the page for another quarter-hour per failure. Now the
worker that hands in the last card stays up and nudges the dispatcher every
minute until the POSTED marker exists.
"""

from datetime import datetime, timedelta, timezone

import carousel
import main
import matchlog


ENTRY = {'match_id': '1', 'home_team': 'A', 'away_team': 'B',
         'carousel_group': 'sat'}
MEMBERS = [dict(ENTRY), {'match_id': '2', 'carousel_group': 'sat'}]


# ── group_status ─────────────────────────────────────────────────────────────

def _status(monkeypatch, posted, landed):
    monkeypatch.setattr(carousel, 'posted_marker',
                        lambda g, e=None: {'ig': 'x'} if posted else None)
    monkeypatch.setattr(carousel, 'read_manifests',
                        lambda g, e=None: {m: {} for m in landed})
    return carousel.group_status('sat', MEMBERS)


def test_posted_wins(monkeypatch):
    assert _status(monkeypatch, True, []) == 'posted'


def test_every_card_in_but_unposted_is_ready(monkeypatch):
    assert _status(monkeypatch, False, ['1', '2']) == 'ready'


def test_a_missing_card_is_waiting(monkeypatch):
    assert _status(monkeypatch, False, ['1']) == 'waiting'


def test_a_failed_listing_reads_as_waiting_not_ready(monkeypatch):
    # read_manifests returns {} on a Cloudinary error.
    assert _status(monkeypatch, False, []) == 'waiting'


# ── The watch ────────────────────────────────────────────────────────────────

def _watch(monkeypatch, statuses, minutes=30):
    """Run the watch over a scripted list of statuses; return what it did."""
    seq = iter(statuses)
    rec = {'nudges': 0, 'alerts': [], 'checks': 0}

    def fake_status(group):
        rec['checks'] += 1
        return next(seq)

    clock = {'t': datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['t']

    def fake_sleep(s):
        clock['t'] += timedelta(seconds=s)

    for name in ('log', 'warn'):
        monkeypatch.setattr(matchlog, name, lambda *a, **k: None)
    monkeypatch.setattr(main, 'datetime', Clock)
    monkeypatch.setattr(main.time, 'sleep', fake_sleep)
    monkeypatch.setattr(main, 'group_status', fake_status)
    monkeypatch.setattr(main, 'nudge_dispatcher',
                        lambda: rec.__setitem__('nudges', rec['nudges'] + 1))
    monkeypatch.setattr(main, 'send_alert',
                        lambda text, **k: rec['alerts'].append(text))

    until = clock['t'] + timedelta(minutes=minutes)
    main._watch_group_publish(dict(ENTRY), until)
    return rec


def test_a_failing_publish_is_nudged_every_minute_until_it_posts(monkeypatch):
    rec = _watch(monkeypatch, ['ready', 'ready', 'ready', 'posted'])
    assert rec['nudges'] == 3
    assert rec['checks'] == 4
    assert not rec['alerts']


def test_already_posted_means_no_nudges(monkeypatch):
    rec = _watch(monkeypatch, ['posted'])
    assert rec['nudges'] == 0


def test_an_incomplete_group_leaves_the_watch_to_the_straggler(monkeypatch):
    rec = _watch(monkeypatch, ['waiting'] * 3)
    assert rec['nudges'] == 0
    assert rec['checks'] == main.GROUP_WATCH_WAITING_POLLS


def test_a_listing_hiccup_does_not_end_the_watch(monkeypatch):
    rec = _watch(monkeypatch, ['waiting', 'ready', 'waiting', 'ready', 'posted'])
    assert rec['nudges'] == 2
    assert not rec['alerts']


def test_running_out_of_time_alerts_once(monkeypatch):
    rec = _watch(monkeypatch, ['ready'] * 10, minutes=5)
    assert rec['nudges'] == 5
    (alert,) = rec['alerts']
    assert alert.startswith('🙋') and "'sat' carousel" in alert


def test_the_worker_starts_the_watch_after_handing_its_card_in(monkeypatch):
    from tests.helpers import goal, scrape

    watched = []
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
        '_archive_match_data': lambda *a, **k: None,
        'is_event_posted': lambda *a: False,
        'send_alert': lambda *a, **k: None,
        '_run_group_pipeline': lambda e, d: True,
        '_watch_group_publish': lambda e, until: watched.append(e['match_id']),
        'get_match_data': lambda url: scrape(
            status='Played', minute='90', fs=('1', '0'), hts=('1', '0'),
            events=[goal(team='A', player='X', minute='10')]),
    }.items():
        monkeypatch.setattr(main, name, fn)
    monkeypatch.setattr(main.time, 'sleep', lambda s: None)

    main.MATCH_STATE.clear()
    kickoff = datetime.now(timezone.utc) - timedelta(minutes=110)
    main.match_worker(dict(ENTRY, scraper_url='http://x',
                           kickoff_utc=kickoff.isoformat()))
    assert watched == ['1']
