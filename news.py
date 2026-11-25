"""
Football news for the Telegram group — post ideas, top-5 leagues only.

Every dispatcher tick (15 minutes) this:
  1. reads the RSS feeds below — four outlets plus a Google News search per
     league — and keeps only items it hasn't seen;
  2. hands them to Gemini as an editor, together with the stories already
     sent in the last two days. Gemini merges the same story from different
     outlets, drops anything outside the Premier League, La Liga, Serie A,
     Bundesliga and Ligue 1, sorts each story into BREAKING or REGULAR, scores
     its importance, flags what's trending, and says when an item is a real
     update to a story already sent (and what changed);
  3. sends breaking news at once (🚨, or 🔄 for an update) and collects the
     rest; once an hour the top five regular stories go out as one message.

A story is never sent twice unless something material has changed. Every
message is headed as a post idea — this is a feed of things worth making a
post about, not a news service.

Memory lives on Cloudinary (news/state.json): what's been seen, what's been
sent, what's waiting for the hourly round-up. No repo commits.

Run it by hand without sending anything:  python news.py --dry-run
"""

import hashlib
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

load_dotenv()

LEAGUES = ('Premier League', 'La Liga', 'Serie A', 'Bundesliga', 'Ligue 1')


def _google_news(query: str) -> str:
    return (f'https://news.google.com/rss/search?q={quote_plus(query)}+when:2h'
            f'&hl=en-GB&gl=GB&ceid=GB:en')


FEEDS = [
    ('BBC Sport', 'https://feeds.bbci.co.uk/sport/football/rss.xml'),
    ('The Guardian', 'https://www.theguardian.com/football/rss'),
    ('Sky Sports', 'https://www.skysports.com/rss/12040'),
    ('ESPN', 'https://www.espn.com/espn/rss/soccer/news'),
] + [(f'Google News · {league}', _google_news(league)) for league in LEAGUES]

RUN_EVERY_SECS = 14 * 60        # the dispatcher can be woken more often than this
DIGEST_EVERY_SECS = 60 * 60     # regular news: once an hour
DIGEST_SIZE = 5
ITEM_WINDOW_SECS = 3 * 3600     # ignore anything older — it isn't news any more
FIRST_RUN_WINDOW_SECS = 3600    # no memory yet: don't flood the group with a backlog
PENDING_TTL_SECS = 3 * 3600     # a regular story still unsent after this is dropped
MEMORY_SECS = 48 * 3600         # how long seen items and sent stories are remembered
MAX_ITEMS_PER_RUN = 80
BREAKING_MIN_IMPORTANCE = 4     # "breaking" but less than this waits for the round-up
MAX_BREAKING_PER_RUN = 3        # a mad deadline night can't flood the group
MAX_KNOWN_STORIES = 60
STATE_ID = 'news/state.json'

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; news-digest/1.0)'}


# ── Reading the feeds ────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


def _item_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).strip().encode()).hexdigest()[:12]


def parse_feed(xml_text: str, feed_name: str) -> list[dict]:
    """RSS → [{id, title, link, source, published, snippet}]. Bad XML → []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter('item'):
        title = _strip_html(node.findtext('title') or '')
        link = (node.findtext('link') or '').strip()
        if not title:
            continue
        source = feed_name
        src = node.find('source')
        if src is not None and (src.text or '').strip():
            source = src.text.strip()            # Google News names the outlet
            title = re.sub(rf'\s+-\s+{re.escape(source)}$', '', title)
        try:
            published = parsedate_to_datetime(node.findtext('pubDate') or '').timestamp()
        except (TypeError, ValueError):
            published = 0
        items.append({
            'id': _item_id(link, title), 'title': title, 'link': link,
            'source': source, 'published': published,
            'snippet': _strip_html(node.findtext('description') or '')[:300],
            'via_google': 'news.google.com' in link,
        })
    return items


GOOGLE_SPACING_SECS = 2         # Google News 429s searches fired together
GOOGLE_RETRY_WAIT_SECS = 8


def _fetch(name: str, url: str, sleep=time.sleep) -> list[dict]:
    """One feed. A Google 429 gets one retry after a pause; any other failure
    just skips the feed for this run."""
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 429 and attempt == 1:
                sleep(GOOGLE_RETRY_WAIT_SECS)
                continue
            r.raise_for_status()
            return parse_feed(r.text, name)
        except Exception as e:
            print(f'[news] {name}: {e}')
            return []
    return []


def fetch_all(sleep=time.sleep) -> list[dict]:
    """Every feed. The outlets in parallel; the Google News searches one at a
    time with a gap, since Google rate-limits a burst. A feed that fails is
    skipped, never fatal."""
    outlets = [f for f in FEEDS if 'news.google.com' not in f[1]]
    searches = [f for f in FEEDS if 'news.google.com' in f[1]]
    with ThreadPoolExecutor(max_workers=max(1, len(outlets))) as pool:
        batches = list(pool.map(lambda f: _fetch(*f, sleep=sleep), outlets))
    for n, (name, url) in enumerate(searches):
        if n:
            sleep(GOOGLE_SPACING_SECS)
        batches.append(_fetch(name, url, sleep=sleep))
    by_id = {}
    for batch in batches:
        for item in batch:
            by_id.setdefault(item['id'], item)
    return list(by_id.values())


# ── Gemini, the editor ───────────────────────────────────────────────────────

EDITOR_BRIEF = f"""\
You are the news editor for an Instagram football page. You receive new news
items from several outlets and the stories already sent to the team in the
last two days. Group the new items into STORIES (one story = one event,
however many outlets report it) and judge each one.

Keep only stories about the top five leagues — {', '.join(LEAGUES)}: their
clubs, players, managers and the leagues themselves (a top-five club's
European tie counts). Set league to "other" for anything else.

category:
  "breaking" — confirmed transfers / "here we go" / official signings,
     managers sacked, appointed or resigning, serious injuries to key
     players, major bans or legal news, records broken, shock results,
     deaths or major off-field events.
  "regular"  — everything else: rumours, quotes, previews, reports, minor
     injuries, opinion.
importance: 1 (minor) to 5 (huge).
trending: true if several outlets cover it or it is clearly a big talking
     point right now.

If a story matches one already sent, give its story_id. Set is_update true
ONLY if the new items add material facts (a rumour became agreed or official,
a fee or contract length confirmed, an injury's length revealed, a new
development) and say what in what_changed. A re-report with nothing new is
is_update false.

headline: neutral, at most 90 characters, no clickbait. summary: one or two
sentences, at most 220 characters. Use only facts in the items — never
invent or assume anything."""

SCHEMA = {
    'type': 'object',
    'properties': {'stories': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {
            'story_id':     {'type': 'string'},
            'item_ids':     {'type': 'array', 'items': {'type': 'string'}},
            'headline':     {'type': 'string'},
            'summary':      {'type': 'string'},
            'league':       {'type': 'string'},
            'category':     {'type': 'string', 'enum': ['breaking', 'regular']},
            'importance':   {'type': 'integer'},
            'trending':     {'type': 'boolean'},
            'is_update':    {'type': 'boolean'},
            'what_changed': {'type': 'string'},
        },
        'required': ['story_id', 'item_ids', 'headline', 'summary', 'league',
                     'category', 'importance', 'trending', 'is_update'],
    }}},
    'required': ['stories'],
}


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M UTC') if ts else '?'


def build_prompt(items: list[dict], known: dict) -> str:
    new = [{'id': i['id'], 'source': i['source'], 'title': i['title'],
            'snippet': i['snippet'], 'published': _hhmm(i['published'])} for i in items]
    sent = [{'story_id': sid, 'headline': s['headline'], 'summary': s['summary'],
             'last_sent': _hhmm(s.get('sent_ts', 0))} for sid, s in known.items()]
    return (EDITOR_BRIEF + '\n\nSTORIES ALREADY SENT:\n' + json.dumps(sent, ensure_ascii=False)
            + '\n\nNEW ITEMS:\n' + json.dumps(new, ensure_ascii=False))


def ask_editor(items: list[dict], known: dict) -> list[dict]:
    """Gemini's stories. Raises if no model answers sensibly."""
    from google.genai import types
    from caption import GEMINI_MODELS, client
    prompt = build_prompt(items, known)
    last = None
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json', response_schema=SCHEMA,
                    temperature=0.2))
            stories = json.loads(response.text).get('stories')
            if not isinstance(stories, list):
                raise ValueError('no stories list')
            return stories
        except Exception as e:
            print(f'[news] {model} failed: {e}')
            last = e
    raise RuntimeError(f'every Gemini model failed ({last})')


# ── Messages ─────────────────────────────────────────────────────────────────

def _sources(story: dict) -> str:
    names = list(dict.fromkeys(story.get('sources') or []))
    return ', '.join(names[:3]) + (f' +{len(names) - 3}' if len(names) > 3 else '')


def _e(text) -> str:
    return html.escape(str(text or ''), quote=False)


def _read_link(story: dict) -> str:
    """A short 'Read on BBC Sport' link — Google News redirects are a screen long."""
    link = story.get('link')
    if not link:
        return ''
    where = (story.get('link_source') or (story.get('sources') or ['the source'])[0])
    return f'<a href="{html.escape(link, quote=True)}">Read on {_e(where)}</a>'


def breaking_message(story: dict, update: bool) -> str:
    """HTML (Telegram parse_mode=HTML)."""
    head = ('🔄 <b>UPDATE — post idea</b>' if update
            else '🚨 <b>BREAKING NEWS — post idea</b>')
    body = _e(story['summary'])
    if update and story.get('what_changed'):
        body = f"<b>What's new:</b> {_e(story['what_changed'])}\n{body}"
    return (f"{head}\n\n<b>{_e(story['headline'])}</b>\n{body}\n\n"
            f"🏆 {_e(story['league'])} · 📰 {_e(_sources(story))}\n{_read_link(story)}").strip()


def digest_message(stories: list[dict]) -> str:
    """HTML (Telegram parse_mode=HTML)."""
    lines = ['📰 <b>REGULAR NEWS — post ideas from the last hour</b>', '']
    for n, s in enumerate(stories, 1):
        flame = '🔥 ' if s.get('trending') else ''
        update = '🔄 ' if s.get('pending_update') else ''
        lines.append(f"{n}. {flame}{update}<b>{_e(s['headline'])}</b>")
        lines.append(_e(s.get('what_changed') if s.get('pending_update') and s.get('what_changed')
                        else s['summary']))
        lines.append(f"🏆 {_e(s['league'])} · {_read_link(s)}")
        lines.append('')
    if any(s.get('trending') or s.get('pending_update') for s in stories):
        lines.append('🔥 trending   🔄 update to an earlier story')
    return '\n'.join(lines).strip()


# ── State ────────────────────────────────────────────────────────────────────

def _cloudinary():
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
                      api_key=os.getenv('CLOUDINARY_API_KEY'),
                      api_secret=os.getenv('CLOUDINARY_API_SECRET'))
    return cloudinary


def load_state() -> dict | None:
    """The saved state, {} if there is none yet, None if it can't be read."""
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(STATE_ID, resource_type='raw')
    except Exception as e:
        return {} if 'not found' in str(e).lower() else None
    try:
        r = requests.get(resource['secure_url'], params={'_': int(time.time())}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def save_state(state: dict) -> None:
    cloudinary = _cloudinary()
    blob = io.BytesIO(json.dumps(state, ensure_ascii=False).encode())
    cloudinary.uploader.upload(blob, resource_type='raw', public_id=STATE_ID,
                               overwrite=True, invalidate=True)


def _prune(state: dict, now: float) -> None:
    cutoff = now - MEMORY_SECS
    state['seen'] = {k: v for k, v in state.get('seen', {}).items() if v >= cutoff}
    state['stories'] = {k: v for k, v in state.get('stories', {}).items()
                        if v.get('last_ts', 0) >= cutoff}
    state['pending'] = [sid for sid in state.get('pending', [])
                        if sid in state['stories']
                        and state['stories'][sid].get('queued_ts', 0) >= now - PENDING_TTL_SECS]


# ── One run ──────────────────────────────────────────────────────────────────

def _story_link(items: list[dict]) -> tuple[str, str]:
    """(link, outlet) — an outlet's own link beats a Google News redirect."""
    direct = [i for i in items if not i.get('via_google') and i.get('link')]
    best = (direct or items or [{}])[0]
    return best.get('link', ''), best.get('source', '')


def apply_stories(state: dict, stories: list[dict], items: list[dict],
                  now: float) -> list[tuple[dict, bool]]:
    """Fold the editor's verdict into state. Returns breaking stories to send
    now, as (story, is_update); regular ones are queued for the round-up."""
    by_item = {i['id']: i for i in items}
    known = state.setdefault('stories', {})
    pending = state.setdefault('pending', [])
    to_send = []
    for s in stories:
        if s.get('league') not in LEAGUES:
            continue
        its = [by_item[i] for i in s.get('item_ids', []) if i in by_item]
        if not its:
            continue
        sid = s.get('story_id') if s.get('story_id') in known else None
        existing = known.get(sid) if sid else None
        update = bool(existing and s.get('is_update'))
        if existing and not update:
            existing['sources'] = list(dict.fromkeys(existing['sources'] + [i['source'] for i in its]))
            existing['last_ts'] = now
            continue
        # A new story's id comes from its own items, so two stories in one run
        # can never collide, whatever their headlines say.
        sid = sid or 's' + hashlib.sha1(
            ','.join(sorted(i['id'] for i in its)).encode()).hexdigest()[:10]
        story = dict(existing or {}, **{
            'headline': s['headline'].strip(), 'summary': s['summary'].strip(),
            'league': s['league'], 'category': s['category'],
            'importance': int(s.get('importance') or 1), 'trending': bool(s.get('trending')),
            'what_changed': (s.get('what_changed') or '').strip() if update else '',
            'sources': list(dict.fromkeys((existing or {}).get('sources', [])
                                          + [i['source'] for i in its])),
            'last_ts': now,
        })
        story['link'], story['link_source'] = _story_link(its)
        known[sid] = story
        if (s['category'] == 'breaking'
                and story['importance'] >= BREAKING_MIN_IMPORTANCE
                and len(to_send) < MAX_BREAKING_PER_RUN):
            story['sent_ts'] = now
            to_send.append((story, update))
        else:
            story.update(queued_ts=now, pending_update=update)
            if sid not in pending:
                pending.append(sid)
    return to_send


def pick_digest(state: dict, now: float) -> list[tuple[str, dict]]:
    stories = state.get('stories', {})
    fresh = [(sid, stories[sid]) for sid in state.get('pending', []) if sid in stories]
    fresh.sort(key=lambda p: (p[1].get('trending', False), p[1].get('importance', 0),
                              p[1].get('queued_ts', 0)), reverse=True)
    return fresh[:DIGEST_SIZE]


def run(now: float | None = None, dry_run: bool = False, force: bool = False,
        send=None) -> list[str]:
    """One tick. Returns the messages sent (or that would be, on a dry run)."""
    now = now or time.time()
    if send is None:
        from telegram_notify import send_news as send
    state = load_state()
    if state is None:
        print('[news] Could not read the saved state — skipping this run.')
        return []
    first_run = not state
    if not force and now - state.get('last_run', 0) < RUN_EVERY_SECS:
        return []
    state['last_run'] = now
    seen = state.setdefault('seen', {})
    messages = []

    window = FIRST_RUN_WINDOW_SECS if first_run else ITEM_WINDOW_SECS
    new = sorted((i for i in fetch_all() if i['id'] not in seen
                  and i['published'] >= now - window),
                 key=lambda i: i['published'], reverse=True)[:MAX_ITEMS_PER_RUN]
    print(f'[news] {len(new)} new item(s).')

    if new:
        recent = dict(sorted(((k, v) for k, v in state.get('stories', {}).items()
                              if v.get('sent_ts') or v.get('queued_ts')),
                             key=lambda kv: kv[1].get('last_ts', 0),
                             reverse=True)[:MAX_KNOWN_STORIES])
        try:
            stories = ask_editor(new, recent)
        except Exception as e:
            # Leave the items unseen: the next run tries them again.
            print(f'[news] Editor unavailable ({e}) — trying again next run.')
            stories = None
        if stories is not None:
            for story, update in apply_stories(state, stories, new, now):
                messages.append((breaking_message(story, update), True))
            for i in new:
                seen[i['id']] = now

    if now - state.get('last_digest', 0) >= DIGEST_EVERY_SECS:
        picks = pick_digest(state, now)
        if picks:
            messages.append((digest_message([s for _sid, s in picks]), False))
            sent = {sid for sid, _s in picks}
            state['pending'] = [sid for sid in state['pending'] if sid not in sent]
            for sid, story in picks:
                story['sent_ts'] = now
                story['pending_update'] = False
            state['last_digest'] = now

    _prune(state, now)
    for text, preview in messages:
        if dry_run:
            print('\n' + '─' * 60 + '\n' + text)
        else:
            send(text, preview=preview)
    if not dry_run:
        save_state(state)
    return [t for t, _p in messages]


if __name__ == '__main__':
    run(dry_run='--dry-run' in sys.argv, force=True)
