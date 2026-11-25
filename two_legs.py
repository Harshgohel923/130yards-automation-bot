"""
Aggregate scores for the second leg of a two-legged knockout tie.

Set on the fixture in matches.json:

    "second_leg": true,
    "first_leg_score": "2-1"

`first_leg_score` is written the way the first leg finished — the first leg's
home team first. In the second leg the teams have swapped ends, so that is
*this* fixture's away team: Liverpool 2-1 Club Brugge in the first leg is
"2-1" on the return fixture Club Brugge vs Liverpool, and the aggregate going
into it is Club Brugge 1-2 Liverpool. validate_matches.py prints that reading
back, so a score typed the wrong way round is caught before match day.

The aggregate is the first leg plus the score *shown on the card* — at half
time the half-time score (a running aggregate), at full time the score after
extra time with shootout goals backed out. A shootout is never part of the
aggregate; it decides a tie the aggregate left level.
"""

import re

_SCORE = re.compile(r'^\s*(\d{1,2})\s*[-–—:x]\s*(\d{1,2})\s*$')


def parse_first_leg(text) -> tuple[int, int] | None:
    """'2-1' → (2, 1): (first leg home goals, first leg away goals)."""
    m = _SCORE.match(str(text or ''))
    return (int(m.group(1)), int(m.group(2))) if m else None


def first_leg_for(entry: dict) -> tuple[int, int] | None:
    """The first leg from this fixture's side — (home goals, away goals) for
    the teams as they line up tonight — or None when this isn't a second leg."""
    if not entry.get('second_leg'):
        return None
    leg = parse_first_leg(entry.get('first_leg_score'))
    if leg is None:
        return None
    first_leg_home, first_leg_away = leg
    # Tonight's home side was the away side in the first leg.
    return first_leg_away, first_leg_home


def aggregate(entry: dict, home_score, away_score) -> tuple[int, int] | None:
    """Tonight's score plus the first leg, from tonight's home side."""
    leg = first_leg_for(entry)
    if leg is None:
        return None
    try:
        return leg[0] + int(home_score), leg[1] + int(away_score)
    except (TypeError, ValueError):
        return None


def shown_score(match_sample: dict, event_type: str) -> tuple[str, str]:
    """The score the card shows: HT score at half time, otherwise the full
    score with any shootout goals taken back out — the renderers' own rule."""
    if event_type == 'HT':
        return (str(match_sample.get('hts_A') or '0'),
                str(match_sample.get('hts_B') or '0'))
    home = str(match_sample.get('fs_A') or match_sample.get('hts_A') or '0')
    away = str(match_sample.get('fs_B') or match_sample.get('hts_B') or '0')
    ps_a = str(match_sample.get('ps_A') or '').strip()
    ps_b = str(match_sample.get('ps_B') or '').strip()
    if ps_a and ps_b:
        try:
            home, away = str(int(home) - int(ps_a)), str(int(away) - int(ps_b))
        except ValueError:
            pass
    return home, away


def annotate(scraper_data: dict, entry: dict, event_type: str) -> None:
    """
    Stamp the aggregate onto matchSample as agg_A / agg_B, where both
    renderers and the caption read it — the same way card_date travels.
    Clears it for anything that isn't a second leg, so a stale value from an
    earlier render can never be shown.
    """
    sample = scraper_data.setdefault('matchSample', {})
    sample.pop('agg_A', None)
    sample.pop('agg_B', None)
    agg = aggregate(entry, *shown_score(sample, event_type))
    if agg is not None:
        sample['agg_A'], sample['agg_B'] = str(agg[0]), str(agg[1])


def aggregate_of(match_sample: dict) -> tuple[str, str] | None:
    """The stamped aggregate, if this render has one."""
    a = str(match_sample.get('agg_A') or '').strip()
    b = str(match_sample.get('agg_B') or '').strip()
    return (a, b) if a and b else None


def result_line(aggregate, penalties) -> str | None:
    """
    The one line under the score.

    Aggregate and shootout share it rather than stacking: there is room for a
    single line between the score and the team names, and both are short.
    Without an aggregate it is the penalties line the cards always had.
    """
    if aggregate and penalties:
        return (f"AGG {aggregate[0]} - {aggregate[1]}  •  "
                f"PENS {penalties[0]} - {penalties[1]}")
    if aggregate:
        return f"AGG {aggregate[0]} - {aggregate[1]}"
    if penalties:
        return f"PENALTIES: {penalties[0]} - {penalties[1]}"
    return None


def tie_summary(home: str, away: str, aggregate, penalties,
                final: bool) -> str | None:
    """
    For the caption: the aggregate and who goes through. A team can win the
    second leg and still be knocked out — the caption must not say otherwise.
    """
    if not aggregate:
        return None
    try:
        a, b = int(aggregate[0]), int(aggregate[1])
    except (TypeError, ValueError):
        return None
    line = f"Aggregate: {home} {a}-{b} {away}"
    if not final:
        if a == b:
            return line + " (tie level on aggregate so far)"
        return line + f" ({home if a > b else away} ahead on aggregate so far)"
    if a != b:
        return line + f" — {home if a > b else away} go through on aggregate"
    if penalties:
        try:
            pa, pb = int(penalties[0]), int(penalties[1])
            return line + (f" — level on aggregate, {home if pa > pb else away} "
                           f"go through on penalties ({pa}-{pb})")
        except (TypeError, ValueError):
            pass
    return line + " — level on aggregate"
