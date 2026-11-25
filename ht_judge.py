"""
Is this first half worth a half-time card? Decided before anyone is asked.

A fixture with "post_ht": false is a match judged not important enough to post
at half time — but now and then its first half is the story. Asking about
every one of them at the whistle buries the one that matters under the ones
that don't, so the worker asks this module first and only sends the Telegram
question on a yes, with the reason at the top.

What counts as worth it (the brief Gemini gets):
  • three or more goals
  • a brace or a hat-trick
  • a red card (a second yellow included)
  • a big upset, given the table or recent form
  • a comeback
  • a record or milestone from the fixture's `records`

The decision, in order:
  1. Hard rules — 4+ goals, or a hat-trick — say yes on their own. An obvious
     one is never left to a model's judgement.
  2. Otherwise Gemini weighs the first half against the brief and answers yes
     or no with a one-line reason.
  3. If Gemini can't be reached (quota, outage), the mechanical half of the
     brief decides — goals, braces, red cards — so a failure never means
     nobody is asked about a first half that plainly deserves it.

The feed has no player ratings: "a player having a great half" can only be
seen through what it records — goals, assists, cards, missed penalties.
"""

import json
from collections import Counter

GOAL_TYPES = ('goal', 'penalty_goal')       # an own goal is nobody's brace
HARD_TOTAL_GOALS = 4
HARD_PLAYER_GOALS = 3
SOFT_TOTAL_GOALS = 3
SOFT_PLAYER_GOALS = 2

CRITERIA = """\
- three or more goals in the half
- a player with a brace or a hat-trick
- a red card (a second yellow counts)
- a big upset, given the league table or recent form (an underdog leading, a
  favourite well beaten)
- a comeback (a side that fell behind now level or ahead)
- a record or milestone mentioned in the fixture's notes"""


# ── What the half produced ───────────────────────────────────────────────────

def _events(scraper_data: dict) -> list[dict]:
    events = scraper_data.get('events')
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _ht_score(scraper_data: dict) -> tuple[int, int]:
    ms = scraper_data.get('matchSample') or {}
    try:
        return int(ms.get('hts_A') or 0), int(ms.get('hts_B') or 0)
    except (TypeError, ValueError):
        return 0, 0


def _player_goals(scraper_data: dict) -> Counter:
    return Counter(e.get('player') or '?' for e in _events(scraper_data)
                   if e.get('type') in GOAL_TYPES)


def _red_cards(scraper_data: dict) -> list[dict]:
    return [e for e in _events(scraper_data) if e.get('type') == 'red_card']


def _ordinal(n: int) -> str:
    return {2: 'brace', 3: 'hat-trick'}.get(n, f'{n} goals')


def hard_reasons(scraper_data: dict) -> list[str]:
    """The rules that say yes whatever Gemini thinks."""
    reasons = []
    home, away = _ht_score(scraper_data)
    if home + away >= HARD_TOTAL_GOALS:
        reasons.append(f"{home + away} goals in the first half")
    for player, n in _player_goals(scraper_data).most_common():
        if n >= HARD_PLAYER_GOALS:
            reasons.append(f"{player} {_ordinal(n)}")
    return reasons


def mechanical_reasons(scraper_data: dict) -> list[str]:
    """The countable half of the brief — what decides when Gemini can't."""
    reasons = []
    home, away = _ht_score(scraper_data)
    if home + away >= SOFT_TOTAL_GOALS:
        reasons.append(f"{home + away} goals in the first half")
    for player, n in _player_goals(scraper_data).most_common():
        if n >= SOFT_PLAYER_GOALS:
            reasons.append(f"{player} {_ordinal(n)}")
    for card in _red_cards(scraper_data):
        what = 'second yellow' if card.get('second_yellow') else 'red card'
        reasons.append(f"{what} for {card.get('player') or '?'} ({card.get('team') or '?'})")
    return reasons


# ── Context for the judgement calls ──────────────────────────────────────────

def _table_line(scraper_data: dict) -> str:
    """'Home 3rd (25 pts) · Away 17th (9 pts)' when the feed carries a table."""
    ms = scraper_data.get('matchSample') or {}
    analysis = scraper_data.get('matchAnalysis')
    table = analysis.get('cup_table') if isinstance(analysis, dict) else None
    rows = table.get('list') if isinstance(table, dict) else None
    if not isinstance(rows, list):
        return ''
    out = []
    for side in ('team_A_name', 'team_B_name'):
        name = ms.get(side)
        row = next((r for r in rows if isinstance(r, dict) and r.get('name') == name), None)
        if row:
            out.append(f"{name} {row.get('rank', '?')} in the table "
                       f"({row.get('points', '?')} pts)")
    return ' · '.join(out)


def build_prompt(scraper_data: dict, entry: dict) -> str:
    from caption import (_summarise_events, _summarise_h2h,
                         _summarise_recent_form, _summarise_stats)
    ms = scraper_data.get('matchSample') or {}
    home = entry.get('home_team') or ms.get('team_A_name', 'Home')
    away = entry.get('away_team') or ms.get('team_B_name', 'Away')
    h, a = _ht_score(scraper_data)
    analysis = scraper_data.get('matchAnalysis')
    analysis = analysis if isinstance(analysis, dict) else {}

    timeline = []
    for e in _events(scraper_data):
        if e.get('type') in ('goal', 'penalty_goal', 'own_goal', 'red_card',
                             'penalty_missed'):
            extra = ' (second yellow)' if e.get('second_yellow') else ''
            assist = f", assist {e['assister']}" if e.get('assister') else ''
            timeline.append(f"{e.get('minute', '?')} {e.get('type')}{extra}: "
                            f"{e.get('player', '?')} ({e.get('team', '?')}){assist}")

    parts = [
        f"Match: {home} vs {away} — {entry.get('competition') or ms.get('competition_name', '')}",
        f"Half-time score: {home} {h}-{a} {away}",
        f"First-half timeline (in order): {'; '.join(timeline) or 'nothing notable'}",
        f"Goals summary: {_summarise_events(scraper_data.get('events', []))}",
    ]
    stats = _summarise_stats(scraper_data.get('statistics', {}))
    if stats:
        parts.append(f"Stats so far: {stats}")
    table = _table_line(scraper_data)
    if table:
        parts.append(f"League table: {table}")
    form = _summarise_recent_form(analysis, home, away)
    if form:
        parts.append(f"Recent form: {form}")
    h2h = _summarise_h2h(analysis)
    if h2h:
        parts.append(f"Head-to-head: {h2h}")
    if entry.get('records'):
        parts.append("Fixture notes (records/milestones): " + '; '.join(map(str, entry['records'])))

    return (
        "You decide whether a football match's FIRST HALF is worth a half-time "
        "scorecard post on an Instagram football page. This match was judged "
        "not important enough to post at half time by default, so the bar is: "
        "would a fan scrolling past stop for this half?\n\n"
        f"It is worth posting if it has at least one of:\n{CRITERIA}\n\n"
        "A routine half — a 1-0, a goalless half, the favourite cruising — is "
        "not worth it. Judge only from the data below; never invent events.\n\n"
        + '\n'.join(parts) +
        "\n\nAnswer with worth_posting (true/false) and reason: one short line "
        "a person reads in a notification, naming the player or the moment "
        "(e.g. 'Kane hat-trick inside 35 minutes', 'bottom side 2-0 up on the "
        "leaders'). If not worth posting, the reason says why in a few words."
    )


# ── The decision ─────────────────────────────────────────────────────────────

def _ask_gemini(prompt: str) -> dict:
    from google.genai import types
    from caption import GEMINI_MODELS, client
    schema = {
        'type': 'object',
        'properties': {'worth_posting': {'type': 'boolean'},
                       'reason': {'type': 'string'}},
        'required': ['worth_posting', 'reason'],
    }
    last_error = None
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_schema=schema, temperature=0.2))
            payload = json.loads(response.text)
            if not isinstance(payload.get('worth_posting'), bool):
                raise ValueError(f'unexpected answer: {response.text[:120]}')
            return {'worth': payload['worth_posting'],
                    'reason': str(payload.get('reason') or '').strip(),
                    'model': model}
        except Exception as e:
            print(f"[ht_judge] {model} failed: {e}")
            last_error = e
    raise RuntimeError(f'every Gemini model failed ({last_error})')


def judge(scraper_data: dict, entry: dict) -> dict:
    """
    {'worth': bool, 'reason': str, 'source': 'rule' | 'gemini' | 'fallback'}.
    Never raises: a decision always comes back.
    """
    hard = hard_reasons(scraper_data)
    if hard:
        return {'worth': True, 'reason': '; '.join(hard), 'source': 'rule'}
    try:
        verdict = _ask_gemini(build_prompt(scraper_data, entry))
        return {'worth': verdict['worth'],
                'reason': verdict['reason'] or ('worth a look' if verdict['worth']
                                                else 'a routine half'),
                'source': 'gemini'}
    except Exception as e:
        print(f"[ht_judge] Gemini unavailable ({e}) — deciding on the rules.")
        soft = mechanical_reasons(scraper_data)
        return {'worth': bool(soft),
                'reason': '; '.join(soft) if soft else 'a routine half (rules only — Gemini unavailable)',
                'source': 'fallback'}
