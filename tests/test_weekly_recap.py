"""
Who sends the photos — counted per person, per week, posted every Monday as a
"Weekly Recap". Re-uploads count too, and are shown separately.
"""

import asyncio
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import leaderboard as lb

BERLIN = ZoneInfo('Europe/Berlin')


def at(iso_local: str) -> float:
    """A moment in German time, as a timestamp."""
    return datetime.fromisoformat(iso_local).replace(tzinfo=BERLIN).timestamp()


# ── What counts, and when ────────────────────────────────────────────────────

@pytest.mark.parametrize('public_id,kind', [
    ('match_photos/54577212_HT', 'HT'), ('match_photos/54577212_FT', 'FT'),
    ('match_photos/54577212_FT_extra_02', 'extra'),
    ('event_photos/54577212_GOAL_kane', 'event'),
    ('scorecards/abc123', None), ('carousel/x/1', None)])
def test_kind_of(public_id, kind):
    assert lb.kind_of(public_id) == kind


def test_weeks_follow_german_time():
    # Sunday 23:30 in Germany is still that week; 00:30 Monday is the next.
    assert lb.week_key(at('2026-11-29T23:30:00')) == '2026-W48'
    assert lb.week_key(at('2026-11-30T00:30:00')) == '2026-W49'


def test_week_range():
    assert lb.week_range('2026-W48') == '23–29 Nov'
    assert lb.week_range('2026-W49') == '30 Nov – 6 Dec'


# ── Counting ─────────────────────────────────────────────────────────────────

def test_counts_by_kind_with_reuploads_and_the_latest_name():
    stats = {}
    t = at('2026-11-25T20:00:00')
    lb.add(stats, 1, 'Harsh', 'FT', False, t)
    lb.add(stats, 1, 'Harsh', 'FT', True, t)          # replaced after a late goal
    lb.add(stats, 1, 'Harsh G', 'event', False, t)
    p = stats['weeks']['2026-W48']['1']
    assert p == {'name': 'Harsh G', 'FT': 2, 'event': 1, 'total': 3, 'reuploads': 1}


def test_only_the_last_eight_weeks_are_kept():
    stats = {}
    for week in range(12):
        lb.add(stats, 1, 'A', 'HT', False, at('2026-09-07T12:00:00') + week * 7 * 86400)
    assert len(stats['weeks']) == lb.KEEP_WEEKS


def test_record_upload_ignores_what_it_cannot_attribute(_in_memory_leaderboard):
    assert lb.record_upload(1, 'A', 'scorecards/generated', False) is False
    assert lb.record_upload(None, 'A', 'match_photos/1_HT', False) is False
    assert _in_memory_leaderboard['stats'] == {}
    assert lb.record_upload(1, 'A', 'match_photos/1_HT', False) is True


def test_unreadable_stats_are_never_overwritten(monkeypatch):
    saved = []
    monkeypatch.setattr(lb, 'load', lambda: None)
    monkeypatch.setattr(lb, 'save', saved.append)
    assert lb.record_upload(1, 'A', 'match_photos/1_HT', False) is False
    assert saved == []


# ── The recap ────────────────────────────────────────────────────────────────

def test_recap_ranks_people_with_medals_and_breakdowns():
    week = {'1': {'name': 'Harsh', 'username': 'Gohellllll', 'FT': 3, 'event': 2,
                  'total': 5, 'reuploads': 1},
            '2': {'name': 'Sam', 'HT': 1, 'total': 1},
            '3': {'name': 'Ana <3', 'extra': 2, 'FT': 1, 'total': 3}}
    text = lb.recap_text(week, '2026-W48')
    assert text.startswith('🏆 <b>Weekly Recap</b> — 23–29 Nov')
    lines = text.splitlines()
    assert ('🥇 <b>Harsh</b> (@Gohellllll) — 5 photos (FT 3 · events 2) '
            '— incl. 1 re-upload') in lines
    assert '🥈 <b>Ana &lt;3</b> — 3 photos (FT 1 · carousel 2)' in lines
    assert '🥉 <b>Sam</b> — 1 photo (HT 1)' in lines            # no username: name only
    assert 'Total: 9 photos from 3 people.' in lines
    assert lines[-1] == 'Thanks all 👏'


def test_recap_adds_how_the_posts_did():
    credit = {'1': {'name': 'Harsh', 'username': 'Gohellllll', 'posts': 6,
                    'likes': 210, 'comments': 9},
              '2': {'name': 'Yash', 'username': 'Yashulllaaa', 'posts': 2,
                    'likes': 1500, 'comments': 1, 'reach': 12000, 'saved': 40}}
    text = lb.recap_text({}, '2026-W48', credit)
    assert '📈 <b>Posts your photos went into</b>' in text
    lines = text.splitlines()
    assert ('🥇 <b>Yash</b> (@Yashulllaaa) — 2 posts · 1,500 likes · 1 comment '
            '· 12,000 reach · 40 saves') in lines
    assert '🥈 <b>Harsh</b> (@Gohellllll) — 6 posts · 210 likes · 9 comments' in lines


def test_a_quiet_week_still_says_so():
    assert 'No photos were sent' in lb.recap_text({}, '2026-W48')


@pytest.mark.parametrize('when,expected', [
    ('2026-11-30T08:59:00', None),            # Monday, too early
    ('2026-11-30T09:00:00', '2026-W48'),      # Monday 09:00 — last week
    ('2026-12-01T10:00:00', None)])           # Tuesday
def test_due_on_monday_morning(when, expected):
    assert lb.due(at(when), {}) == expected


def test_sent_once_per_week(_in_memory_leaderboard):
    sent = []
    lb.add(_in_memory_leaderboard['stats'], 1, 'Harsh', 'FT', False, at('2026-11-25T20:00:00'))
    send = lambda text: sent.append(text) or True
    assert lb.maybe_send_weekly_recap(now=at('2026-11-30T09:05:00'), send=send)
    assert not lb.maybe_send_weekly_recap(now=at('2026-11-30T09:20:00'), send=send)
    assert len(sent) == 1 and 'Harsh' in sent[0]


def test_a_failed_send_tries_again(_in_memory_leaderboard):
    results = iter([False, True])
    sent = []
    send = lambda text: sent.append(text) or next(results)
    assert not lb.maybe_send_weekly_recap(now=at('2026-11-30T09:05:00'), send=send)
    assert lb.maybe_send_weekly_recap(now=at('2026-11-30T09:20:00'), send=send)
    assert len(sent) == 2


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_the_bot_counts_an_upload_for_whoever_sent_it(_in_memory_leaderboard, monkeypatch):
    import telegram_bot as tb

    class TgFile:
        async def download_to_drive(self, path):
            open(path, 'wb').write(b'x')

    class Bot:
        async def get_file(self, file_id):
            return TgFile()

    class Msg:
        async def reply_text(self, *a, **k):
            pass

    monkeypatch.setattr(tb.cloudinary.uploader, 'upload', lambda path, **kw: {
        'public_id': kw['public_id'], 'secure_url': 'u', 'overwritten': True})
    monkeypatch.setattr(tb, '_shrink_for_upload', lambda p: False)
    ctx = types.SimpleNamespace(user_data={}, bot=Bot(),
                                chat_data={'last_user': 'Harsh', 'last_user_id': 7,
                                           'last_username': 'gohel'})
    match = {'match_id': '42', 'home_team': 'A', 'away_team': 'B'}
    assert asyncio.run(tb._upload_photo(Msg(), ctx, match, 'FT', ('f', 1)))
    week = next(iter(_in_memory_leaderboard['stats']['weeks'].values()))
    assert week['7'] == {'name': 'Harsh', 'username': 'gohel', 'FT': 1, 'total': 1,
                         'reuploads': 1}


def test_the_dispatcher_sends_the_recap(monkeypatch):
    import dispatcher
    calls = []
    monkeypatch.setattr(lb, 'maybe_send_weekly_recap', lambda: calls.append(1) or False)
    dispatcher.publish_weekly_recap()
    assert calls == [1]


# ── Everyone appears — and zero means LOSER ──────────────────────────────────

GROUP = [{'id': '1329280363', 'name': 'Harsh Gohel', 'username': 'Gohellllll'},
         {'id': '1244357046', 'name': 'Kishan Jaiswal', 'username': 'Jswlkishann'},
         {'id': '1271500818', 'name': 'Yash', 'username': 'Yashulllaaa'}]


def test_everyone_appears_and_zero_is_a_loser():
    week = {'1329280363': {'name': 'Harsh', 'FT': 2, 'total': 2}}
    credit = {'1271500818': {'name': 'Yash', 'username': 'Yashulllaaa', 'posts': 1,
                             'likes': 9, 'comments': 0}}
    lines = lb.recap_text(week, '2026-W48', credit, GROUP).splitlines()
    photos = lines[lines.index('📸 <b>Photos sent through the bot</b>'):]
    assert '🥇 <b>Harsh</b> (@Gohellllll) — 2 photos (FT 2)' in photos
    assert '🫵 <b>LOSER</b>: <b>Kishan Jaiswal</b> (@Jswlkishann) — 0 photos' in photos
    assert '🫵 <b>LOSER</b>: <b>Yash</b> (@Yashulllaaa) — 0 photos' in photos
    assert 'Total: 2 photos from 1 person.' in photos          # losers don't count
    posts = lines[lines.index('📈 <b>Posts your photos went into</b>'):]
    assert '🥇 <b>Yash</b> (@Yashulllaaa) — 1 post · 9 likes · 0 comments' in posts
    assert '🫵 <b>LOSER</b>: <b>Harsh Gohel</b> (@Gohellllll) — 0 posts' in posts
    assert '🫵 <b>LOSER</b>: <b>Kishan Jaiswal</b> (@Jswlkishann) — 0 posts' in posts


def test_losers_come_after_everyone_with_a_score():
    week = {'1244357046': {'name': 'Kishan', 'HT': 1, 'total': 1}}
    lines = lb.recap_text(week, '2026-W48', {}, GROUP).splitlines()
    first_loser = next(i for i, l in enumerate(lines) if 'LOSER' in l)
    medal = next(i for i, l in enumerate(lines) if l.startswith('🥇'))
    assert medal < first_loser


def test_a_whole_week_of_losers():
    text = lb.recap_text({}, '2026-W48', {}, GROUP)
    assert text.count('LOSER') == 6                  # three people, both sections
    assert 'Total: 0 photos from 0 people.' in text


def test_the_roster_keeps_someone_telegram_could_not_find(monkeypatch):
    monkeypatch.setenv('TELEGRAM_ALLOWED_USER_IDS', '1, 2')
    people = lb.roster(lookup=lambda uid: GROUP[0] if uid == '1' else None)
    assert people == [GROUP[0], {'id': '2', 'name': 'user 2', 'username': ''}]


def test_the_monday_recap_includes_the_whole_group(_in_memory_leaderboard, monkeypatch):
    monkeypatch.setattr(lb, 'roster', lambda: GROUP)
    sent = []
    lb.maybe_send_weekly_recap(now=at('2026-11-30T09:05:00'),
                               send=lambda text: sent.append(text) or True)
    assert sent[0].count('LOSER') == 6
    assert '@Jswlkishann' in sent[0]
