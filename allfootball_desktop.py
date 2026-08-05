# allfootball_desktop.py
"""
Secondary data source: allfootball's desktop match page.

The mobile page (football_scraper_dom.py) stays the source of truth — it is the
only one carrying half-time, extra-time and penalty scorelines, which the
scorecard is built from. The desktop page is strictly additive, supplying two
things the mobile feed does not have:

  * minute_extra — the stoppage-time offset of an event. Mobile reports a
    90th-minute booking and a 90+3 booking identically as "90'"; desktop
    distinguishes them.
  * tendencies — the per-minute momentum series, archived with the match data.

Everything here is best-effort. Every entry point returns None or leaves the
match data untouched on failure, so a desktop outage can never affect posting.

The page is a Nuxt app: its state lives in a `window.__NUXT__` assignment which
is a minified JavaScript IIFE, not JSON. decode_nuxt() evaluates it with the
standard library alone — the body is a plain object literal whose values are
either literals or identifiers bound to the call's arguments, so resolving it
is a matter of substituting the arguments and quoting the keys.
"""

import json
import re

import requests

DESKTOP_URL = 'https://www.allfootballapp.com/match/{match_id}'
REQUEST_TIMEOUT = 10
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36')

_NUXT_RE = re.compile(r'window\.__NUXT__=(.*?)</script>', re.S)
_IDENT_RE = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')

# Desktop event codes → the substitution slot they occupy. Subs are two rows
# on the desktop side (in and out) but a single event on the mobile side.
_SUB_IN, _SUB_OUT = 'SI', 'SO'


def decode_nuxt(js: str):
    """
    Decode a `window.__NUXT__` IIFE into Python data.

    Raises ValueError if the payload is not the shape we expect, which callers
    treat as "no desktop data this time".
    """
    js = js.strip().rstrip(';')
    try:
        sig_at = js.index('function(') + len('function(')
        params = [p.strip() for p in js[sig_at:js.index(')', sig_at)].split(',')]
        body_at = js.index('{return ') + len('{return ')
        tail_at = js.rindex('}(')
    except ValueError as e:
        raise ValueError(f'unexpected __NUXT__ shape: {e}') from e

    body = js[body_at:tail_at].rstrip()
    args = json.loads('[' + js[tail_at + 2:].rstrip().rstrip(')').rstrip() + ']')
    env = dict(zip(params, args))

    # Walk the object literal, quoting bare keys and resolving identifiers to
    # their argument values. String literals are copied verbatim so that
    # identifier-looking text inside them is never rewritten.
    out, i, n = [], 0, len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if body[j] == '\\':
                    j += 2
                    continue
                if body[j] == '"':
                    break
                j += 1
            out.append(body[i:j + 1])
            i = j + 1
            continue
        m = _IDENT_RE.match(body, i)
        if not m:
            out.append(ch)
            i += 1
            continue
        word, end = m.group(), m.end()
        if end < n and body[end] == ':':
            out.append(json.dumps(word))          # object key
        elif word in env:
            out.append(json.dumps(env[word]))     # argument reference
        elif word in ('true', 'false', 'null'):
            out.append(word)
        else:
            out.append('null')                    # undefined identifier
        i = end
    return json.loads(''.join(out))


def fetch_desktop_data(match_id: str) -> dict | None:
    """The desktop page's match payload, or None if anything goes wrong."""
    url = DESKTOP_URL.format(match_id=match_id)
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT,
                         headers={'User-Agent': USER_AGENT})
        r.raise_for_status()
        m = _NUXT_RE.search(r.text)
        if not m:
            print(f'[desktop] No __NUXT__ payload in {url}')
            return None
        data = decode_nuxt(m.group(1))
        return (data.get('data') or [None])[0]
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f'[desktop] Could not read {url}: {e}')
        return None


def format_minute(event: dict) -> str:
    """
    Display form of an event's minute, folding in the stoppage-time offset that
    enrich() attached: "90'" plus minute_extra "3" renders as "90+3'".

    Events with no offset — the overwhelming majority, and every event when the
    desktop source is unavailable — come back untouched, so renderers can call
    this unconditionally.
    """
    raw = str(event.get('minute') or '?')
    extra = str(event.get('minute_extra') or '').strip()
    if not extra or extra == '0':
        return raw
    m = re.match(r"\s*(\d+)\s*('?)\s*$", raw)
    if not m:
        return raw
    return f"{m.group(1)}+{extra}{m.group(2)}"


def _minute_of(raw) -> int | None:
    """Leading minute of '90', \"90'\" or '90+3' as an int."""
    m = re.match(r'\s*(\d+)', str(raw or ''))
    return int(m.group(1)) if m else None


def _extra_index(desktop: dict) -> dict[tuple[int, str, bool], str]:
    """
    Map (minute, person_id, is_sub_out) → minute_extra, for every desktop event
    carrying a non-zero stoppage offset.

    The sub-out flag disambiguates the two rows a substitution produces, which
    share a minute but name different players.
    """
    index: dict[tuple[int, str, bool], str] = {}
    events = (desktop.get('overview') or {}).get('events')
    if not isinstance(events, dict):
        return index
    for bucket in events.values():
        if not isinstance(bucket, dict):
            continue
        minute = _minute_of(bucket.get('minute'))
        if minute is None:
            continue
        for side in ('teamAEvents', 'teamBEvents'):
            for ev in bucket.get(side) or []:
                extra = str(ev.get('minute_extra') or '0').strip()
                pid = str(ev.get('person_id') or '').strip()
                if not pid or extra in ('', '0'):
                    continue
                index[(minute, pid, ev.get('code') == _SUB_OUT)] = extra
    return index


def enrich(scraper_data: dict, match_id: str, desktop: dict | None = None) -> bool:
    """
    Add desktop-only detail to a mobile scrape, in place.

    Adds 'minute_extra' to any event that happened in stoppage time, and
    attaches the tendencies series as scraper_data['tendencies'].

    Returns True if anything was added. Never raises: on any failure the match
    data is left exactly as it was, so posting proceeds on mobile data alone.
    """
    try:
        if desktop is None:
            desktop = fetch_desktop_data(match_id)
        if not desktop:
            return False

        changed = False

        index = _extra_index(desktop)
        events = scraper_data.get('events')
        # Before kickoff the scraper returns a placeholder string here, not a
        # list — iterating that would walk it character by character.
        if index and isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                minute = _minute_of(ev.get('minute'))
                if minute is None:
                    continue
                # Substitutions carry two ids; the rest carry one.
                for pid_key, is_out in (('player_id', False),
                                        ('player_in_id', False),
                                        ('player_out_id', True)):
                    pid = str(ev.get(pid_key) or '').strip()
                    if not pid:
                        continue
                    extra = index.get((minute, pid, is_out))
                    if extra:
                        ev['minute_extra'] = extra
                        changed = True
                        break

        tendencies = (desktop.get('overview') or {}).get('tendencies')
        if tendencies:
            scraper_data['tendencies'] = tendencies
            changed = True

        return changed
    except Exception as e:                     # never let enrichment break a post
        print(f'[desktop] Enrichment skipped for {match_id}: {e}')
        return False
