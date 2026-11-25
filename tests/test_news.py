"""
Football news for the group — post ideas, top-5 leagues only (news.py).

Feeds are read every tick; Gemini edits them into stories; breaking news goes
out at once, regular news as an hourly top five; a story is never sent twice
unless it has a real update.
"""

import json
import types

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

    first_google = {'done': False}

    def get(url, **k):
        calls.append(url)
        if 'news.google.com' in url and not first_google['done']:
            first_google['done'] = True
            return R(429)
        return R(200)

    monkeypatch.setattr(news.requests, 'get', get)
    items = news.fetch_all(sleep=sleeps.append)
    google_calls = [c for c in calls if 'news.google.com' in c]
    assert len(google_calls) == len(news.LEAGUES) + 1           # one retry
    assert news.GOOGLE_RETRY_WAIT_SECS in sleeps
    assert sleeps.count(news.GOOGLE_SPACING_SECS) == len(news.LEAGUES) - 1
    assert len(items) == 2                                       # deduplicated


# ── Folding the editor's verdict into state ──────────────────────────────────

def item(i, source='BBC Sport', google=False):
    return {'id': f'i{i}', 'title': f't{i}', 'link': f'https://x/{i}', 'source': source,
            'published': NOW, 'snippet': '', 'via_google': google}


def story(ids, category='regular', importance=3, league='Premier League', **kw):
    return dict({'story_id': '', 'item_ids': ids, 'headline': 'H', 'summary': 'S',
                 'league': league, 'category': category, 'importance': importance,
                 'trending': False, 'is_update': False}, **kw)


def test_other_leagues_are_dropped():
    state = {}
    sent = news.apply_stories(state, [story(['i1'], league='other')], [item(1)], NOW)
    assert sent == [] and state['stories'] == {} and state['pending'] == []


def test_big_breaking_goes_now_small_breaking_waits():
    state = {}
    sent = news.apply_stories(state, [story(['i1'], 'breaking', 5),
                                      story(['i2'], 'breaking', 3)],
                              [item(1), item(2)], NOW)
    assert len(sent) == 1 and sent[0][1] is False
    assert len(state['pending']) == 1          # the importance-3 one


def test_at_most_three_breaking_per_run():
    state = {}
    stories = [story([f'i{n}'], 'breaking', 5) for n in range(5)]
    sent = news.apply_stories(state, stories, [item(n) for n in range(5)], NOW)
    assert len(sent) == news.MAX_BREAKING_PER_RUN
    assert len(state['pending']) == 2


def test_a_re_report_is_not_sent_again():
    state = {}
    news.apply_stories(state, [story(['i1'], 'breaking', 5)], [item(1)], NOW)
    sid = next(iter(state['stories']))
    sent = news.apply_stories(state, [story(['i2'], 'breaking', 5, story_id=sid)],
                              [item(2, source='Sky Sports')], NOW + 900)
    assert sent == []
    assert state['stories'][sid]['sources'] == ['BBC Sport', 'Sky Sports']


def test_a_real_update_is_sent_as_an_update():
    state = {}
    news.apply_stories(state, [story(['i1'], 'breaking', 5)], [item(1)], NOW)
    sid = next(iter(state['stories']))
    sent = news.apply_stories(state, [story(['i2'], 'breaking', 5, story_id=sid,
                                            is_update=True, what_changed='Now official')],
                              [item(2)], NOW + 900)
    (s, update), = sent
    assert update and s['what_changed'] == 'Now official'


def test_an_outlets_own_link_beats_google():
    state = {}
    news.apply_stories(state, [story(['i1', 'i2'])],
                       [item(1, 'Yahoo', google=True), item(2, 'BBC Sport')], NOW)
    s = next(iter(state['stories'].values()))
    assert s['link'] == 'https://x/2' and s['link_source'] == 'BBC Sport'


def test_digest_puts_trending_and_important_first():
    state = {'stories': {
        'a': {'importance': 2, 'trending': False, 'queued_ts': NOW},
        'b': {'importance': 4, 'trending': False, 'queued_ts': NOW},
        'c': {'importance': 1, 'trending': True, 'queued_ts': NOW}},
        'pending': ['a', 'b', 'c']}
    assert [sid for sid, _s in news.pick_digest(state, NOW)] == ['c', 'b', 'a']


# ── Messages ─────────────────────────────────────────────────────────────────

def test_messages_are_safe_html_with_short_links():
    s = {'headline': 'Spurs <3 win', 'summary': 'A & B', 'league': 'Premier League',
         'sources': ['BBC Sport', 'Sky', 'ESPN', 'Guardian'], 'link': 'https://x/?a=1&b=2',
         'link_source': 'BBC Sport', 'trending': True}
    msg = news.breaking_message(s, update=False)
    assert msg.startswith('🚨 <b>BREAKING NEWS — post idea</b>')
    assert '<b>Spurs &lt;3 win</b>' in msg and 'A &amp; B' in msg
    assert '<a href="https://x/?a=1&amp;b=2">Read on BBC Sport</a>' in msg
    assert 'BBC Sport, Sky, ESPN +1' in msg
    d = news.digest_message([s])
    assert d.startswith('📰 <b>REGULAR NEWS — post ideas from the last hour</b>')
    assert '1. 🔥 <b>Spurs &lt;3 win</b>' in d


def test_update_message_says_what_changed():
    s = {'headline': 'H', 'summary': 'S', 'league': 'Serie A', 'sources': ['x'],
         'link': '', 'what_changed': 'Fee agreed'}
    assert "🔄 <b>UPDATE — post idea</b>" in news.breaking_message(s, update=True)
    assert "<b>What's new:</b> Fee agreed" in news.breaking_message(s, update=True)


def test_telegram_gets_html_without_previews_for_the_digest(monkeypatch):
    import telegram_notify as tn
    sent = {}
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 't')
    monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', 'alerts')
    monkeypatch.setenv('TELEGRAM_NEWS_CHAT_ID', 'news-group')
    monkeypatch.setattr(tn.requests, 'post', lambda url, data=None, timeout=None:
                        sent.update(data) or types.SimpleNamespace(ok=True))
    assert tn.send_news('<b>x</b>')
    assert sent['parse_mode'] == 'HTML' and sent['chat_id'] == 'news-group'
    assert sent['disable_web_page_preview'] == 'true'


# ── A whole run ──────────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch):
    w = types.SimpleNamespace(state={}, items=[], stories=[], sent=[], editor_fails=False,
                              editor_calls=0)
    monkeypatch.setattr(news, 'load_state', lambda: json.loads(json.dumps(w.state)))
    monkeypatch.setattr(news, 'save_state', lambda s: setattr(w, 'state', s))
    monkeypatch.setattr(news, 'fetch_all', lambda: list(w.items))

    def editor(items, known):
        w.editor_calls += 1
        if w.editor_fails:
            raise RuntimeError('503')
        return w.stories
    monkeypatch.setattr(news, 'ask_editor', editor)
    w.send = lambda text, preview=False: w.sent.append(text)
    return w


def test_first_run_sends_breaking_and_a_digest(world):
    world.items = [item(1), item(2)]
    world.stories = [story(['i1'], 'breaking', 5), story(['i2'])]
    out = news.run(now=NOW, send=world.send)
    assert len(out) == 2 and out[0].startswith('🚨') and out[1].startswith('📰')
    assert set(world.state['seen']) == {'i1', 'i2'}


def test_a_run_inside_fifteen_minutes_does_nothing(world):
    world.state = {'last_run': NOW - 60, 'seen': {}}
    world.items = [item(1)]
    assert news.run(now=NOW, send=world.send) == []
    assert world.editor_calls == 0


def test_regular_news_waits_for_the_hour(world):
    world.state = {'last_run': NOW - 3600, 'last_digest': NOW - 1800, 'seen': {}}
    world.items = [item(1)]
    world.stories = [story(['i1'])]
    assert news.run(now=NOW, send=world.send) == []        # queued, not sent
    world.items, world.stories = [], []
    out = news.run(now=NOW + 1800, send=world.send)
    assert len(out) == 1 and out[0].startswith('📰')


def test_seen_items_are_not_sent_to_the_editor_again(world):
    world.state = {'last_run': 0, 'seen': {'i1': NOW - 60}}
    world.items = [item(1)]
    news.run(now=NOW, send=world.send)
    assert world.editor_calls == 0


def test_old_items_are_ignored(world):
    world.state = {'last_run': 0, 'seen': {}}
    old = dict(item(1), published=NOW - news.ITEM_WINDOW_SECS - 60)
    world.items = [old]
    news.run(now=NOW, send=world.send)
    assert world.editor_calls == 0


def test_an_editor_failure_leaves_items_for_next_run(world):
    world.state = {'last_run': 0, 'seen': {}}
    world.items = [item(1)]
    world.editor_fails = True
    assert news.run(now=NOW, send=world.send) == []
    assert 'i1' not in world.state['seen']


def test_unreadable_state_skips_the_run(world, monkeypatch):
    monkeypatch.setattr(news, 'load_state', lambda: None)
    world.items = [item(1)]
    assert news.run(now=NOW, send=world.send) == []
    assert world.editor_calls == 0


def test_the_dispatcher_runs_news_and_can_switch_it_off(monkeypatch):
    import dispatcher
    ran = []
    monkeypatch.setattr(news, 'run', lambda: ran.append(1) or [])
    dispatcher.publish_news()
    monkeypatch.setenv('NEWS_ENABLED', '0')
    dispatcher.publish_news()
    assert ran == [1]
