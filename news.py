"""
Football news for the Telegram group — post ideas, top-5 leagues only.

Every dispatcher tick (15 minutes) this reads the RSS feeds below — four
outlets plus a Google News search per league — and files every headline it
hasn't seen in an inbox. Then, outside match hours:

  • BREAKING (within 15 minutes): only if a new headline carries a breaking
    word (official, signs, sacked, injury, "here we go", …) is Gemini asked —
    about those headlines alone — whether any is truly breaking. Up to three
    go out at once (🚨, or 🔄 for an update to a story already sent).
  • REGULAR (every 3 hours): one Gemini call picks the top five stories from
    everything collected since the last round-up, sent as one message.

Results are never news here: the page already posts a scorecard for those.
Headlines with a scoreline or result words are dropped in code before Gemini
sees them, and Gemini is told to drop anything about a match result too.

During match hours — for each day's fixtures in matches.json, from the
earliest kickoff to the latest expected end — nothing is sent and Gemini
isn't called; headlines keep being collected and the next run after the
window catches up.

A story is never sent twice unless something material has changed. Every
message is headed as a post idea.

Memory lives on Cloudinary (news/state.json). No repo commits.
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
from datetime import datetime, timedelta, timezone
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
DIGEST_EVERY_SECS = 3 * 3600    # regular news: every three hours
DIGEST_SIZE = 5
ITEM_WINDOW_SECS = 3 * 3600     # an item older than this when first seen isn't news
FIRST_RUN_WINDOW_SECS = 3600    # no memory yet: don't flood the group with a backlog
INBOX_TTL_SECS = 12 * 3600      # long enough to outlast a full matchday window
INBOX_MAX = 200
MEMORY_SECS = 48 * 3600         # seen items and sent stories
MAX_BREAKING_CHECK = 40         # headlines per breaking check
MAX_DIGEST_ITEMS = 100          # headlines per round-up
BREAKING_MIN_IMPORTANCE = 4
MAX_BREAKING_PER_RUN = 3
STATE_ID = 'news/state.json'

# Match length used for the quiet window — the worker's own "can't still be
# playing" figures (main.py's scraper outage fallback).
MATCH_SECS = 115 * 60
KNOCKOUT_MATCH_SECS = 165 * 60

HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; news-digest/1.0)'}
REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'matches.json')


# ── Results are not news here ────────────────────────────────────────────────
# A scoreline (2-1, 3–0) — never a fee or a date. "£51m" or "2026-27" can't match.
_SCORELINE = re.compile(r'(?<![\d£€$.,/-])\b\d{1,2}\s?[-–]\s?\d{1,2}\b(?![\d/-])')
_RESULT_WORDS = re.compile(
    r'\b(beat|beats|beaten|thrash|thrashed|rout|routs|routed|stun|stuns|stunned|'
    r'draw|draws|drew|win over|victory|defeat|defeated|comeback win|'
    r'highlights|player ratings|match report|as it happened|recap|reaction:|'
    r'live:|live updates|full-time|half-time|talking points)\b', re.I)


def is_result_story(title: str) -> bool:
    """A match result, report or live blog — the scorecard post covers it.
    Never true for a headline with a breaking word: "Chelsea sack manager after
    3-0 defeat" is breaking news, so Gemini (told to drop results) decides."""
    if is_breaking_candidate(title):
        return False
    return bool(_SCORELINE.search(title or '') or _RESULT_WORDS.search(title or ''))


# Headlines worth a breaking check. Everything else waits for the round-up.
_BREAKING_WORDS = re.compile(
    r'\b(official|officially|confirm|confirms|confirmed|here we go|signs|signed|'
    r'signing|completes?|joins|agree|agrees|agreed|agreement|deal done|medical|'
    r'sacked|sack|fired|'
    r'dismissed|appointed|appoints|new (head )?coach|new manager|resigns?|'
    r'steps down|parts ways|injury|injured|ruptured|torn|acl|surgery|fracture|'
    r'out for (the )?season|ruled out|banned|ban|suspended|suspension|charged|'
    r'arrested|court|dies|died|death|passed away|retires?|retirement|record)\b',
    re.I)


def is_breaking_candidate(title: str) -> bool:
    return bool(_BREAKING_WORDS.search(title or ''))


# ── Match hours ──────────────────────────────────────────────────────────────

def match_windows(registry: list[dict]) -> list[tuple[float, float]]:
    """[(start, end)] per matchday: the earliest kickoff to the latest expected
    end among the fixtures sharing a date in German time (the bot's local
    time), so an overnight kickoff is its own window."""
    from config import LOCAL_TZ
    days: dict = {}
    for entry in registry or []:
        try:
            ko = datetime.fromisoformat(str(entry['kickoff_utc']).replace('Z', '+00:00'))
        except (KeyError, TypeError, ValueError):
            continue
        length = KNOCKOUT_MATCH_SECS if entry.get('knockout_match') else MATCH_SECS
        start, end = ko.timestamp(), ko.timestamp() + length
        day = ko.astimezone(LOCAL_TZ).date()
        lo, hi = days.get(day, (start, end))
        days[day] = (min(lo, start), max(hi, end))
    return sorted(days.values())


def in_match_window(now: float, registry: list[dict]) -> bool:
    return any(start <= now <= end for start, end in match_windows(registry))


def _load_registry() -> list[dict]:
    try:
        with open(REGISTRY_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


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
    """One feed. A 429 gets one retry after a pause; any other failure just
    skips the feed for this run."""
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

_COMMON = f"""\
You are the news editor for an Instagram football page. Group the news items
into STORIES (one story = one event, however many outlets report it).

Keep only stories about the top five leagues — {', '.join(LEAGUES)}: their
clubs, players, managers and the leagues themselves. Set league to "other"
for anything else.

NEVER include match results, scorelines, wins, losses, draws, comebacks,
match reports, player ratings or highlights — the page already posts a
scorecard for every result. Set league to "other" for those too.

If a story matches one already sent, give its story_id, and set is_update
true ONLY if the items add material facts (a rumour became agreed or
official, a fee or contract length confirmed, an injury's length revealed);
say what in what_changed. A re-report with nothing new: is_update false.

importance: 1 (minor) to 5 (huge). trending: true if several outlets cover it
or it is clearly a big talking point. headline: neutral, at most 90
characters, no clickbait. summary: one or two sentences, at most 220
characters. Use only facts in the items — never invent or assume anything."""

BREAKING_BRIEF = _COMMON + """

You are checking headlines for BREAKING news only. category "breaking" means:
confirmed transfers / "here we go" / official signings, managers sacked,
appointed or resigning, serious injuries to key players, major bans or legal
news, records broken, deaths or major off-field events. Everything else is
"regular" — it will wait for the round-up."""

DIGEST_BRIEF = _COMMON + """

You are picking stories for a round-up of the last few hours: transfer
rumours and developments, injuries, quotes and press conferences, managerial
news, off-field stories. Return the stories worth a post, with category
"regular"."""

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


def build_prompt(brief: str, items: list[dict], known: dict) -> str:
    new = [{'id': i['id'], 'source': i['source'], 'title': i['title'],
            'snippet': i.get('snippet', ''), 'published': _hhmm(i['published'])}
           for i in items]
    sent = [{'story_id': sid, 'headline': s['headline'], 'summary': s['summary'],
             'last_sent': _hhmm(s.get('sent_ts', 0))} for sid, s in known.items()]
    return (brief + '\n\nSTORIES ALREADY SENT:\n' + json.dumps(sent, ensure_ascii=False)
            + '\n\nNEWS ITEMS:\n' + json.dumps(new, ensure_ascii=False))


def ask_editor(brief: str, items: list[dict], known: dict) -> list[dict]:
    """Gemini's stories. Raises if no model answers sensibly."""
    from google.genai import types
    from caption import GEMINI_MODELS, client
    prompt = build_prompt(brief, items, known)
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
    lines = ['📰 <b>REGULAR NEWS — post ideas from the last 3 hours</b>', '']
    for n, s in enumerate(stories, 1):
        flame = '🔥 ' if s.get('trending') else ''
        update = '🔄 ' if s.get('is_update') else ''
        lines.append(f"{n}. {flame}{update}<b>{_e(s['headline'])}</b>")
        lines.append(_e(s.get('what_changed') if s.get('is_update') and s.get('what_changed')
                        else s['summary']))
        lines.append(f"🏆 {_e(s['league'])} · {_read_link(s)}")
        lines.append('')
    if any(s.get('trending') or s.get('is_update') for s in stories):
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
    inbox = {k: v for k, v in state.get('inbox', {}).items()
             if v.get('added', 0) >= now - INBOX_TTL_SECS}
    newest = sorted(inbox.items(), key=lambda kv: kv[1].get('published', 0),
                    reverse=True)[:INBOX_MAX]
    state['inbox'] = dict(newest)


# ── Folding Gemini's stories into state ──────────────────────────────────────

def _story_link(items: list[dict]) -> tuple[str, str]:
    """(link, outlet) — an outlet's own link beats a Google News redirect."""
    direct = [i for i in items if not i.get('via_google') and i.get('link')]
    best = (direct or items or [{}])[0]
    return best.get('link', ''), best.get('source', '')


def apply_stories(state: dict, stories: list[dict], items: list[dict],
                  now: float, mode: str) -> list[tuple[dict, bool]]:
    """
    Record the editor's stories. Returns the ones to send as (story, update):
      mode 'breaking' — breaking, importance 4+, at most three
      mode 'digest'   — the best five (trending, then importance)
    A story already sent comes back only as a real update.
    """
    by_item = {i['id']: i for i in items}
    known = state.setdefault('stories', {})
    picked = []
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
        if mode == 'breaking' and not (s.get('category') == 'breaking'
                                       and int(s.get('importance') or 0) >= BREAKING_MIN_IMPORTANCE):
            continue        # not breaking: it stays in the inbox for the round-up
        # A new story's id comes from its own items, so two stories in one run
        # can never collide, whatever their headlines say.
        sid = sid or 's' + hashlib.sha1(
            ','.join(sorted(i['id'] for i in its)).encode()).hexdigest()[:10]
        story = dict(existing or {}, **{
            'headline': s['headline'].strip(), 'summary': s['summary'].strip(),
            'league': s['league'], 'importance': int(s.get('importance') or 1),
            'trending': bool(s.get('trending')), 'is_update': update,
            'what_changed': (s.get('what_changed') or '').strip() if update else '',
            'sources': list(dict.fromkeys((existing or {}).get('sources', [])
                                          + [i['source'] for i in its])),
            'item_ids': [i['id'] for i in its], 'last_ts': now,
        })
        story['link'], story['link_source'] = _story_link(its)
        picked.append((sid, story, update))

    if mode == 'breaking':
        picked = picked[:MAX_BREAKING_PER_RUN]
    else:
        picked.sort(key=lambda p: (p[1]['trending'], p[1]['importance']), reverse=True)
        picked = picked[:DIGEST_SIZE]
    for sid, story, _u in picked:
        story['sent_ts'] = now
        known[sid] = story
    return [(story, update) for _sid, story, update in picked]


# ── One run ──────────────────────────────────────────────────────────────────

def _recent_stories(state: dict) -> dict:
    return dict(sorted(state.get('stories', {}).items(),
                       key=lambda kv: kv[1].get('last_ts', 0), reverse=True)[:60])


def run(now: float | None = None, dry_run: bool = False, force: bool = False,
        send=None, registry: list[dict] | None = None) -> list[str]:
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
    inbox = state.setdefault('inbox', {})
    messages = []

    # Collect — always, even during match hours: the next run after the window
    # catches up from here.
    window = FIRST_RUN_WINDOW_SECS if first_run else ITEM_WINDOW_SECS
    fresh = [i for i in fetch_all() if i['id'] not in seen
             and i['published'] >= now - window]
    for i in fresh:
        seen[i['id']] = now
        if is_result_story(i['title']):
            continue
        inbox[i['id']] = dict(i, added=now, checked=not is_breaking_candidate(i['title']))
    print(f'[news] {len(fresh)} new headline(s), inbox {len(inbox)}.')

    if in_match_window(now, _load_registry() if registry is None else registry):
        print('[news] Match hours — collecting only, nothing sent.')
    else:
        # Breaking: only the new headlines with a breaking word, only if any.
        candidates = sorted((i for i in inbox.values() if not i.get('checked')),
                            key=lambda i: i['published'], reverse=True)[:MAX_BREAKING_CHECK]
        if candidates:
            try:
                stories = ask_editor(BREAKING_BRIEF, candidates, _recent_stories(state))
            except Exception as e:
                print(f'[news] Breaking check unavailable ({e}) — next run.')
            else:
                for story, update in apply_stories(state, stories, candidates, now, 'breaking'):
                    messages.append((breaking_message(story, update), True))
                    for iid in story['item_ids']:
                        inbox.pop(iid, None)        # told already — not round-up material
                for i in candidates:
                    if i['id'] in inbox:
                        inbox[i['id']]['checked'] = True

        # Regular: one round-up every three hours.
        if inbox and now - state.get('last_digest', 0) >= DIGEST_EVERY_SECS:
            pool = sorted(inbox.values(), key=lambda i: i['published'],
                          reverse=True)[:MAX_DIGEST_ITEMS]
            try:
                stories = ask_editor(DIGEST_BRIEF, pool, _recent_stories(state))
            except Exception as e:
                print(f'[news] Round-up unavailable ({e}) — next run.')
            else:
                picks = apply_stories(state, stories, pool, now, 'digest')
                if picks:
                    messages.append((digest_message([s for s, _u in picks]), False))
                state['inbox'] = {}
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
