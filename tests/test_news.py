"""
Football news for the group — post ideas, top-5 leagues only (news.py).

Headlines are collected every tick. Outside match hours: Gemini checks only
the new headlines with a breaking word (🚨 within 15 minutes), and once every
three hours picks a top five from everything collected (📰). Results and
scorelines are never news here. A story is never sent twice unless updated.
"""

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

import news


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Arsenal sign striker &amp; more</title>
  <link>https://www.bbc.co.uk/sport/football/1</link>
  <pubDate>Wed, 25 Nov 2026 21:00:00 GMT</pubDate>
  <description>&lt;p&gt;Done deal.&lt;/p&gt;</description></item>
<item><title>Bayern stunned - Yahoo Sports</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <pubDate>Wed, 25 Nov 2026 21:30:00 GMT</pubDate>
  <source url="https://sports.yahoo.com">Yahoo Sports</source></item>
</channel></rss>"""

NOW = 1795644000.0   # 2026-11-25 22:00 UTC


def test_parse_feed():
    a, b = news.parse_feed(RSS, 'BBC Sport')
    assert a['title'] == 'Arsenal sign striker & more' and a['source'] == 'BBC Sport'
    assert a['snippet'] == 'Done deal.' and not a['via_google']
    assert b['title'] == 'Bayern stunned'                 # " - Outlet" removed
    assert b['source'] == 'Yahoo Sports' and b['via_google']
    assert b['published'] == pytest.approx(NOW - 1800)
    assert news.parse_feed('not xml', 'x') == []


def test_google_searches_go_one_at_a_time_and_a_429_is_retried(monkeypatch):
    calls, sleeps = [], []

    class R:
        def __init__(self, status):
            self.status_code, self.text = status, RSS

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    first = {'done': False}

    def get(url, **k):
        calls.append(url)
        if 'news.google.com' in url and not first['done']:
            first['done'] = True
            return R(429)
        return R(200)

    monkeypatch.setattr(news.requests, 'get', get)
    items = news.fetch_all(sleep=sleeps.append)
    assert len([c for c in calls if 'news.google.com' in c]) == len(news.LEAGUES) + 1
    assert news.GOOGLE_RETRY_WAIT_SECS in sleeps
    assert sleeps.count(news.GOOGLE_SPACING_SECS) == len(news.LEAGUES) - 1
    assert len(items) == 2


# ── What counts as a result, what counts as breaking ─────────────────────────

@pytest.mark.parametrize('title', [
    'Sabah 1-4 Barcelona', 'Liverpool beat Club Brugge in Champions League',
    'Champions League highlights: Inter v Stuttgart', 'Player ratings as Man Utd draw',
    'Roma end PSG run with 2–2 draw', 'Match report: Arsenal v Spurs'])
def test_results_are_dropped(title):
    assert news.is_result_story(title)


@pytest.mark.parametrize('title', [
    'Arsenal agree £51m fee for striker', 'Club confirm 2026-27 kit',
    'Coach explains his 4-3-3 shift', 'Talks held with Arsenal over new deal',
    'PSG eye January move for Athletic star'])
def test_other_news_is_kept(title):
    assert not news.is_result_story(title)


def test_breaking_words_survive_a_scoreline():
    title = 'Chelsea sack manager after 3-0 defeat'
    assert news.is_breaking_candidate(title) and not news.is_result_story(title)


@pytest.mark.parametrize('title,expected', [
    ('Kane ruled out for six weeks', True), ('Here we go! Wirtz to Liverpool', True),
    ('Ancelotti appointed Brazil coach', True), ('PSG eye January move', False)])
def test_breaking_candidates(title, expected):
    assert news.is_breaking_candidate(title) is expected


# ── Match hours ──────────────────────────────────────────────────────────────

def ko(iso, **kw):
    return dict({'match_id': iso, 'kickoff_utc': iso}, **kw)


def ts(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


def test_one_window_per_matchday_from_first_kickoff_to_last_end():
    reg = [ko('2026-11-28T12:30:00Z'), ko('2026-11-28T17:30:00Z'),
           ko('2026-11-28T15:00:00Z')]
    (start, end), = news.match_windows(reg)
    assert start == ts('2026-11-28T12:30:00Z')
    assert end == ts('2026-11-28T17:30:00Z') + news.MATCH_SECS


def test_a_knockout_runs_longer():
    (_s, end), = news.match_windows([ko('2026-11-28T20:00:00Z', knockout_match=True)])
    assert end == ts('2026-11-28T20:00:00Z') + news.KNOCKOUT_MATCH_SECS


def test_an_overnight_kickoff_is_its_own_window():
    # 01:30 UTC is 02:30 German time — the next local day.
    reg = [ko('2026-11-28T20:00:00Z'), ko('2026-11-29T01:30:00Z')]
    assert len(news.match_windows(reg)) == 2
    assert not news.in_match_window(ts('2026-11-28T23:30:00Z'), reg)   # the gap
    assert news.in_match_window(ts('2026-11-29T02:00:00Z'), reg)


def test_inside_and_outside():
    reg = [ko('2026-11-28T15:00:00Z')]
    assert news.in_match_window(ts('2026-11-28T16:00:00Z'), reg)
    assert not news.in_match_window(ts('2026-11-28T14:59:00Z'), reg)
    assert not news.in_match_window(ts('2026-11-28T17:00:00Z'), reg)


# ── Folding the editor's verdict into state ──────────────────────────────────

def item(i, title='Kane ruled out', source='BBC Sport', google=False, published=NOW):
    return {'id': f'i{i}', 'title': title, 'link': f'https://x/{i}', 'source': source,
            'published': published, 'snippet': '', 'via_google': google}


def story(ids, category='regular', importance=3, league='Premier League', **kw):
    return dict({'story_id': '', 'item_ids': ids, 'headline': 'H', 'summary': 'S',
                 'league': league, 'category': category, 'importance': importance,
                 'trending': False, 'is_update': False}, **kw)


def test_breaking_mode_sends_only_big_breaking_news():
    state = {}
    sent = news.apply_stories(state, [story(['i1'], 'breaking', 5),
                                      story(['i2'], 'breaking', 3),
                                      story(['i3'], 'regular', 5),
                                      story(['i4'], 'breaking', 5, league='other')],
                              [item(n) for n in (1, 2, 3, 4)], NOW, 'breaking')
    assert len(sent) == 1
    assert len(state['stories']) == 1          # only what was sent is remembered


def test_at_most_three_breaking_per_run():
    sent = news.apply_stories({}, [story([f'i{n}'], 'breaking', 5) for n in range(5)],
                              [item(n) for n in range(5)], NOW, 'breaking')
    assert len(sent) == news.MAX_BREAKING_PER_RUN


def test_digest_mode_takes_the_best_five():
    stories = [story([f'i{n}'], importance=n % 5 + 1, trending=(n == 0)) for n in range(8)]
    picks = news.apply_stories({}, stories, [item(n) for n in range(8)], NOW, 'digest')
    assert len(picks) == news.DIGEST_SIZE
    assert picks[0][0]['trending']             # trending first
    assert [p[0]['importance'] for p in picks[1:]] == sorted(
        [p[0]['importance'] for p in picks[1:]], reverse=True)


def test_a_re_report_is_not_sent_again():
    state = {}
    news.apply_stories(state, [story(['i1'], 'breaking', 5)], [item(1)], NOW, 'breaking')
    sid = next(iter(state['stories']))
    sent = news.apply_stories(state, [story(['i2'], story_id=sid)],
                              [item(2, source='Sky Sports')], NOW + 900, 'digest')
    assert sent == []
    assert state['stories'][sid]['sources'] == ['BBC Sport', 'Sky Sports']


def test_a_real_update_is_sent():
    state = {}
    news.apply_stories(state, [story(['i1'], 'breaking', 5)], [item(1)], NOW, 'breaking')
    sid = next(iter(state['stories']))
    (s, update), = news.apply_stories(
        state, [story(['i2'], 'breaking', 5, story_id=sid, is_update=True,
                      what_changed='Now official')], [item(2)], NOW + 900, 'breaking')
    assert update and s['what_changed'] == 'Now official'


def test_an_outlets_own_link_beats_google():
    (s, _u), = news.apply_stories({}, [story(['i1', 'i2'])],
                                  [item(1, source='Yahoo', google=True), item(2)], NOW, 'digest')
    assert s['link'] == 'https://x/2' and s['link_source'] == 'BBC Sport'


# ── Messages ─────────────────────────────────────────────────────────────────

def test_messages_are_safe_html_with_short_links():
    s = {'headline': 'Spurs <3 deal', 'summary': 'A & B', 'league': 'Premier League',
         'sources': ['BBC Sport', 'Sky', 'ESPN', 'Guardian'], 'link': 'https://x/?a=1&b=2',
         'link_source': 'BBC Sport', 'trending': True}
    msg = news.breaking_message(s, update=False)
    assert msg.startswith('🚨 <b>BREAKING NEWS — post idea</b>')
    assert '<b>Spurs &lt;3 deal</b>' in msg and 'A &amp; B' in msg
    assert '<a href="https://x/?a=1&amp;b=2">Read on BBC Sport</a>' in msg
    d = news.digest_message([s])
    assert d.startswith('📰 <b>REGULAR NEWS — post ideas from the last 3 hours</b>')
    assert '1. 🔥 <b>Spurs &lt;3 deal</b>' in d


def test_telegram_gets_html_without_previews_for_the_digest(monkeypatch):
    import telegram_notify as tn
    sent = {}
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 't')
    monkeypatch.setenv('TELEGRAM_NEWS_CHAT_ID', 'news-group')
    monkeypatch.setattr(tn.requests, 'post', lambda url, data=None, timeout=None:
                        sent.update(data) or types.SimpleNamespace(ok=True))
    assert tn.send_news('<b>x</b>')
    assert sent['parse_mode'] == 'HTML' and sent['chat_id'] == 'news-group'
    assert sent['disable_web_page_preview'] == 'true'


# ── Whole runs ───────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(state={'last_run': 0, 'last_digest': NOW, 'seen': {}},
                              items=[], breaking=[], digest=[], sent=[], calls=[],
                              fail=False)
    monkeypatch.setattr(news, 'load_state', lambda: json.loads(json.dumps(w.state)))
    monkeypatch.setattr(news, 'save_state', lambda s: setattr(w, 'state', s))
    monkeypatch.setattr(news, 'fetch_all', lambda: list(w.items))

    def editor(brief, items, known):
        mode = 'breaking' if brief is news.BREAKING_BRIEF else 'digest'
        w.calls.append((mode, [i['id'] for i in items]))
        if w.fail:
            raise RuntimeError('503')
        return w.breaking if mode == 'breaking' else w.digest
    monkeypatch.setattr(news, 'ask_editor', editor)
    w.send = lambda text, preview=False: w.sent.append(text)
    w.run = lambda now=NOW, registry=(): news.run(now=now, send=w.send,
                                                  registry=list(registry))
    return w


def test_no_breaking_words_means_no_gemini_at_all(world):
    world.items = [item(1, 'PSG eye January move'), item(2, 'Manager praises squad')]
    assert world.run() == [] and world.calls == []
    assert set(world.state['inbox']) == {'i1', 'i2'}      # kept for the round-up


def test_breaking_check_sees_only_the_candidates(world):
    world.items = [item(1, 'Kane ruled out for six weeks'), item(2, 'PSG eye move')]
    world.breaking = [story(['i1'], 'breaking', 5)]
    out = world.run()
    assert world.calls == [('breaking', ['i1'])]
    assert len(out) == 1 and out[0].startswith('🚨')
    assert set(world.state['inbox']) == {'i2'}             # the sent one is gone


def test_a_candidate_is_checked_once(world):
    world.items = [item(1, 'Kane ruled out for six weeks')]
    world.run()
    world.run(now=NOW + 900)
    assert [c[0] for c in world.calls] == ['breaking']


def test_results_never_reach_the_inbox(world):
    world.items = [item(1, 'Sabah 1-4 Barcelona'), item(2, 'Liverpool beat Brugge')]
    world.run()
    assert world.state['inbox'] == {} and world.calls == []


def test_the_round_up_comes_every_three_hours(world):
    world.items = [item(1, 'PSG eye move')]
    world.digest = [story(['i1'])]
    assert world.run(now=NOW + 2 * 3600) == []
    world.items = []
    out = world.run(now=NOW + 3 * 3600)
    assert len(out) == 1 and out[0].startswith('📰')
    assert world.calls == [('digest', ['i1'])]
    assert world.state['inbox'] == {}


def test_match_hours_collect_but_send_nothing(world):
    reg = [ko(datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat())]
    world.items = [item(1, 'Kane ruled out for six weeks'), item(2, 'PSG eye move')]
    world.breaking = [story(['i1'], 'breaking', 5)]
    assert world.run(now=NOW + 3 * 3600 - 7000, registry=reg) == []   # inside the window
    assert world.calls == [] and len(world.state['inbox']) == 2


def test_after_match_hours_it_catches_up(world):
    reg = [ko(datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat())]
    world.items = [item(1, 'Kane ruled out for six weeks')]
    world.breaking = [story(['i1'], 'breaking', 5)]
    world.run(now=NOW, registry=reg)
    world.items = []
    out = world.run(now=NOW + 3 * 3600, registry=reg)        # window over
    assert out and out[0].startswith('🚨')


def test_a_gemini_failure_retries_next_run(world):
    world.items = [item(1, 'Kane ruled out for six weeks')]
    world.fail = True
    world.run()
    assert world.state['inbox']['i1']['checked'] is False
    world.fail, world.items = False, []
    world.breaking = [story(['i1'], 'breaking', 5)]
    assert world.run(now=NOW + 900)


def test_old_headlines_are_ignored(world):
    world.items = [item(1, 'Kane ruled out', published=NOW - news.ITEM_WINDOW_SECS - 60)]
    world.run()
    assert world.state['inbox'] == {}


def test_a_run_inside_fifteen_minutes_does_nothing(world):
    world.state['last_run'] = NOW - 60
    world.items = [item(1, 'Kane ruled out')]
    assert world.run() == [] and world.calls == []


def test_unreadable_state_skips_the_run(world, monkeypatch):
    monkeypatch.setattr(news, 'load_state', lambda: None)
    world.items = [item(1, 'Kane ruled out')]
    assert world.run() == [] and world.calls == []


def test_the_dispatcher_runs_news_and_can_switch_it_off(monkeypatch):
    import dispatcher
    ran = []
    monkeypatch.setattr(news, 'run', lambda: ran.append(1) or [])
    dispatcher.publish_news()
    monkeypatch.setenv('NEWS_ENABLED', '0')
    dispatcher.publish_news()
    assert ran == [1]
