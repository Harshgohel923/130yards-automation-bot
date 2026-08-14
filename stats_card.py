# stats_card.py — the full-time match statistics carousel page
"""
Second slide of a single-match FT post: the numbers behind the scoreline.

The page deliberately does not repeat the score — slide one already carries it.
Instead the header is the momentum chart, and the body is a priority-ordered
list of statistics drawn as team-coloured split bars.

Design notes
────────────
  * Same template as the scorecard, fetched through scorecard.load_template so
    the two slides can never land on different backgrounds. Here it is blurred
    and veiled: the page is dense with small marks, and a sharp stadium photo
    behind them is unreadable.
  * Momentum comes from allfootball_desktop's `tendencies` series, which is
    best-effort by design. When it is missing the header degrades to a
    crest–VS–crest strip and the rest of the page is unaffected.
  * The scraper publishes wildly different stat sets — seven keys for a
    friendly, nearly thirty for a big game — so STAT_PRIORITY picks the most
    interesting ones that are actually present and the row height adapts to
    however many that turns out to be.
  * Bar colours reuse the overlay card's crest sampling, with a luminance floor
    (a black or navy team would otherwise vanish into the background) and a
    minimum mutual distance (two red teams would otherwise draw identical bars).

generate_stats_card() returns None when the match has no statistics at all,
which the caller treats as "post the scorecard on its own".
"""

import os

from PIL import Image, ImageDraw, ImageFilter

from config import (get_brand_logo_url, get_crest_url, LOCAL_SYMBOLS,
                    TEAM_NAME_ALIASES)
from overlay_scorebar import _dominant_colors, FALLBACK_COLORS, TEAM_COLORS
from scorecard import (_draw_centered_text, _draw_crest, _draw_text_at_size,
                       _font, _shadow_of, load_template, FONT_BOLD,
                       COLOR_NAME, COLOR_STADIUM, COLOR_TITLE)

# ── Statistics shown, in priority order ──────────────────────────────────────
# Scraper key → row label. The first MAX_ROWS of these that the match actually
# has are drawn; the tail exists so a thin friendly stat set still fills the
# page instead of leaving a gap.
STAT_PRIORITY = (
    ('Possession',        'POSSESSION'),
    ('Shots',             'SHOTS'),
    ('Shots On Target',   'SHOTS ON TARGET'),
    ('Big Chances',       'BIG CHANCES'),
    ('Corners',           'CORNERS'),
    ('Pass accuracy',     'PASS ACCURACY'),
    ('Goalkeeper Saves',  'SAVES'),
    ('Fouls',             'FOULS'),
    ('Offsides',          'OFFSIDES'),
    ('Yellow cards',      'YELLOW CARDS'),
    ('Dangerous Attacks', 'DANGEROUS ATTACKS'),
    ('Attacks',           'ATTACKS'),
    ('Passes',            'PASSES'),
)
MAX_ROWS = 9

# ── BOUNDING BOXES (x1, y1, x2, y2) ─────────────────────────────────────────
# Same 1086x1448 canvas as the scorecard templates.
BRAND_LOGO_BOX = (56, 70, 120, 135)      # 130 Yards mark
TITLE_BOX      = (284, 158, 802, 252)    # 'MATCH STATS'
MOM_LABEL_BOX  = (431, 286, 731, 320)    # 'MOMENTUM', centred over the chart
CHART_BOX      = (176, 336, 986, 512)    # momentum plot area
HOME_KEY_BOX   = (78, 348, 146, 416)     # home crest, level with its half
AWAY_KEY_BOX   = (78, 436, 146, 504)     # away crest, level with its half
FOOTER_BOX     = (362, 1396, 724, 1428)  # competition name

# Stat rows fill the space between these, however many there are.
BLOCK_TOP    = 574
BLOCK_BOTTOM = 1362
BLOCK_L      = 108
BLOCK_R      = 978

# Fallback header when there is no momentum series.
FB_HOME_CREST_BOX = (300, 336, 420, 456)
FB_AWAY_CREST_BOX = (666, 336, 786, 456)
FB_VS_BOX         = (480, 366, 606, 426)
FB_HOME_NAME_BOX  = (180, 466, 540, 502)
FB_AWAY_NAME_BOX  = (546, 466, 906, 502)

# ── COLOURS ───────────────────────────────────────────────────────────────────
COLOR_MUTED = (150, 160, 178, 255)   # losing value, axis labels
COLOR_AXIS  = (255, 255, 255, 46)
COLOR_TRACK = (255, 255, 255, 32)    # unfilled part of a stat bar
COLOR_HT_RULE = (255, 255, 255, 60)

# ── BACKGROUND ────────────────────────────────────────────────────────────────
BG_BLUR   = 16    # Gaussian radius applied to the template
BG_DARKEN = 96    # alpha of the flat veil over the blurred photo

# ── SIZING ────────────────────────────────────────────────────────────────────
TITLE_FONT_MAX  = 120
TITLE_FONT_MIN  = 24
LABEL_FONT_MAX  = 38
VALUE_FONT_BUMP = 6     # values sit a little larger than their row label
MOMENTUM_MARGIN = 32    # headroom each side of the axis, for goal markers
GOAL_SYMBOL_PX  = 22
BAR_MIN_PX      = 12
BAR_GAP_PX      = 3     # gap where the home and away halves of a bar meet
COLOR_MIN_LUM   = 95    # below this a bar colour is lifted until it reads
COLOR_MIN_DIST  = 120   # minimum channel distance between the two bar colours

GOAL_TYPES = ('goal', 'penalty_goal', 'own_goal')


# ── Small helpers ─────────────────────────────────────────────────────────────

def _num(value) -> float:
    """Stat values arrive as ints or as '64%' strings — both become floats."""
    try:
        return float(str(value).replace('%', '').strip() or 0)
    except ValueError:
        return 0.0


def _luminance(color) -> float:
    return 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]


def _lift(color, floor=COLOR_MIN_LUM):
    """Brighten a colour until it reads against the dark, veiled background."""
    color = tuple(int(v) for v in color[:3])
    while _luminance(color) < floor:
        color = tuple(min(255, int(v * 1.25) + 26) for v in color)
    return color


def _distance(a, b) -> int:
    return sum(abs(x - y) for x, y in zip(a[:3], b[:3]))


def _team_palette(team_name: str, crest):
    """Curated colours where we have them, sampled from the crest otherwise —
    the same resolution both card styles already use."""
    curated = TEAM_COLORS.get(team_name)
    if curated:
        return curated
    if crest is None:
        return FALLBACK_COLORS
    try:
        return _dominant_colors(crest)
    except Exception:
        return FALLBACK_COLORS


def bar_colors(home_palette, away_palette):
    """One readable colour per team: bright enough for the background, and far
    enough from each other that the two halves of a bar are distinguishable."""
    home = _lift(home_palette[0])
    for candidate in list(away_palette) + [(232, 84, 96), (240, 244, 250)]:
        away = _lift(candidate)
        if _distance(home, away) >= COLOR_MIN_DIST:
            return home, away
    return home, (240, 244, 250)


def pick_stats(stat_list: dict, max_rows: int = MAX_ROWS) -> list[tuple]:
    """(label, home value, away value) for the top stats this match published."""
    rows = []
    if not isinstance(stat_list, dict):
        return rows
    for key, label in STAT_PRIORITY:
        if len(rows) >= max_rows:
            break
        value = stat_list.get(key)
        if isinstance(value, dict):
            rows.append((label, str(value.get('home', '0')),
                         str(value.get('away', '0'))))
    return rows


def _load_crest_image(name: str):
    """Team crest as a PIL image, or None — used for colours and the key."""
    url = get_crest_url(name, alert=False)
    if not url:
        return None
    try:
        import io

        import requests
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert('RGBA')
    except Exception as e:
        print(f"[stats] Could not load crest for {name}: {e}")
        return None


def _paste_centred(img, crest, box):
    if crest is None:
        return
    crest = crest.copy()
    bw, bh = box[2] - box[0], box[3] - box[1]
    crest.thumbnail((bw, bh), Image.LANCZOS)
    img.paste(crest, (box[0] + (bw - crest.width) // 2,
                      box[1] + (bh - crest.height) // 2), crest)


def _rounded_bar(draw, x0, y0, x1, y1, color):
    """A pill, skipped entirely when the value rounds it out of existence."""
    if x1 <= x0:
        return
    draw.rounded_rectangle((x0, y0, x1, y1),
                           radius=min((y1 - y0) / 2, (x1 - x0) / 2), fill=color)


# ── Goal markers ──────────────────────────────────────────────────────────────

def _minute_int(raw) -> int | None:
    """"90+3'" → 90. None when the minute is unparsable."""
    try:
        return int(str(raw or '').replace("'", '').split('+')[0].strip())
    except ValueError:
        return None


def _index_minute(i: int, ht_index: int | None) -> int:
    """
    Tendencies index → match minute.

    The series runs one slot per minute with the interval inserted at HT, so
    slots before it sit one minute behind the clock.
    """
    return i + 1 if (ht_index is not None and i < ht_index) else i


def goal_types_for_side(events, team_name: str, marker_indexes, ht_index) -> list[str]:
    """
    One event type per momentum goal marker, so each can draw its own symbol.

    The tendencies feed only says 'a goal happened at this slot' — the type has
    to come from the mobile events list. Each marker takes that team's nearest
    unclaimed goal by minute; when the minutes don't line up (a stoppage-time
    goal the two sources place differently) it falls back to chronological
    order, and to a plain goal when there is nothing left to match.
    """
    marker_indexes = list(marker_indexes)
    if not isinstance(events, list):
        return ['goal'] * len(marker_indexes)

    scored = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get('type') in GOAL_TYPES and event.get('team') == team_name:
            minute = _minute_int(event.get('minute'))
            if minute is not None:
                scored.append((minute, event['type']))

    used, types = set(), []
    for index in marker_indexes:
        minute = _index_minute(index, ht_index)
        best, best_gap = None, None
        for j, (event_minute, _) in enumerate(scored):
            if j in used:
                continue
            gap = abs(event_minute - minute)
            if best_gap is None or gap < best_gap:
                best, best_gap = j, gap

        if best is not None and best_gap <= 3:
            used.add(best)
            types.append(scored[best][1])
            continue

        nxt = next((j for j in range(len(scored)) if j not in used), None)
        if nxt is None:
            types.append('goal')
        else:
            used.add(nxt)
            types.append(scored[nxt][1])
    return types


def load_goal_symbols(size: int = GOAL_SYMBOL_PX) -> dict:
    """Event type → symbol image, the same files the scorer lines use."""
    symbols = {}
    for event_type, key in (('goal', 'normal_goal'),
                            ('penalty_goal', 'penalty_goal'),
                            ('own_goal', 'own_goal')):
        path = LOCAL_SYMBOLS.get(key, '')
        if not path or not os.path.exists(path):
            continue
        try:
            symbol = Image.open(path).convert('RGBA')
            symbol.thumbnail((size, size), Image.LANCZOS)
            symbols[event_type] = symbol
        except Exception:
            continue
    return symbols


# ── Momentum chart ────────────────────────────────────────────────────────────

def draw_momentum(img, draw, tendencies, home_color, away_color,
                  events, raw_home, raw_away):
    """
    Per-minute momentum around a centre line: home above, away below.

    Positive y in the feed means the home side, negative the away side, each
    scaled to ±100. Goals are marked at the tip of the scoring team's bar.
    """
    x0, y0, x1, y1 = CHART_BOX
    mid = (y0 + y1) / 2
    half = (y1 - y0) / 2 - MOMENTUM_MARGIN

    points = tendencies.get('data') or []
    if not points:
        return
    step = (x1 - x0) / len(points)
    bar_width = max(3.0, step * 0.6)

    draw.line((x0, mid, x1, mid), fill=COLOR_AXIS, width=2)

    ht_index = next((i for i, p in enumerate(points)
                     if str(p.get('minute', '')).upper() == 'HT'), None)
    if ht_index is not None:
        hx = x0 + step * (ht_index + 0.5)
        for y in range(int(y0), int(y1), 12):
            draw.line((hx, y, hx, y + 6), fill=COLOR_HT_RULE, width=2)

    for i, point in enumerate(points):
        y = _num(point.get('y'))
        if y == 0:
            continue
        cx = x0 + step * (i + 0.5)
        height = max(bar_width, abs(y) / 100 * half)
        color = (*home_color, 235) if y > 0 else (*away_color, 235)
        top = mid - height if y > 0 else mid + 3
        bottom = mid - 3 if y > 0 else mid + height
        draw.rounded_rectangle((cx - bar_width / 2, top, cx + bar_width / 2, bottom),
                               radius=bar_width / 2, fill=color)

    symbols = load_goal_symbols()
    for side, key, team in ((1, 'team_a_goal', raw_home),
                            (-1, 'team_b_goal', raw_away)):
        indexes = [i for i, p in enumerate(points)
                   if any(e.get('code') == 'G' for e in (p.get(key) or []))]
        placed = []
        for index, event_type in zip(indexes,
                                     goal_types_for_side(events, team, indexes, ht_index)):
            cx = x0 + step * (index + 0.5)
            height = abs(_num(points[index].get('y'))) / 100 * half
            gy = mid - height - 15 if side > 0 else mid + height + 15
            symbol = symbols.get(event_type) or symbols.get('goal')
            if symbol is None:
                draw.ellipse((cx - 7, gy - 7, cx + 7, gy + 7), fill=COLOR_TITLE)
                continue
            # Goals a minute apart would draw on top of each other. Slide the
            # later one sideways — stacking would run it into the minute labels.
            gy = min(max(gy, y0 + symbol.height / 2), y1 - symbol.height / 2)
            for px, py in placed:
                if abs(cx - px) < symbol.width + 3 and abs(gy - py) < symbol.height + 3:
                    cx = px + symbol.width + 3
            placed.append((cx, gy))
            img.paste(symbol, (int(cx - symbol.width / 2),
                               int(gy - symbol.height / 2)), symbol)

    # Minute ticks — only the slots the feed itself labels (0/15/30/HT/60/75/90).
    for i, point in enumerate(points):
        label = str(point.get('minute') or '').strip()
        if not label:
            continue
        cx = x0 + step * (i + 0.5)
        _draw_text_at_size(draw, label.replace("'", ''),
                           (int(cx - 34), int(y1 + 14), int(cx + 34), int(y1 + 42)),
                           FONT_BOLD, 24, COLOR_MUTED)


def draw_team_strip(img, draw, home_team, away_team, home_crest, away_crest):
    """Header fallback when the momentum series is unavailable."""
    _paste_centred(img, home_crest, FB_HOME_CREST_BOX)
    _paste_centred(img, away_crest, FB_AWAY_CREST_BOX)
    _draw_centered_text(draw, 'VS', FB_VS_BOX, FONT_BOLD, 60, 18, COLOR_MUTED)
    _draw_centered_text(draw, home_team.upper(), FB_HOME_NAME_BOX,
                        FONT_BOLD, 34, 12, COLOR_NAME)
    _draw_centered_text(draw, away_team.upper(), FB_AWAY_NAME_BOX,
                        FONT_BOLD, 34, 12, COLOR_NAME)


# ── Page ──────────────────────────────────────────────────────────────────────

def generate_stats_card(scraper_data: dict, match_id_override: str = '',
                        home_name: str | None = None, away_name: str | None = None,
                        competition: str | None = None) -> str | None:
    """
    Build the statistics page for a finished match.

    Returns the local path of the saved PNG, or None when the match published no
    statistics — the caller falls back to posting the scorecard on its own.
    """
    match_sample = scraper_data.get('matchSample', {})
    raw_home = match_sample.get('team_A_name', 'Home')
    raw_away = match_sample.get('team_B_name', 'Away')
    # Display / crest names: the validated matches.json names win over the
    # scraper's spelling; raw names stay for matching events to a team.
    home_team = home_name or TEAM_NAME_ALIASES.get(raw_home, raw_home)
    away_team = away_name or TEAM_NAME_ALIASES.get(raw_away, raw_away)
    match_id = str(match_sample.get('match_id') or match_id_override or 'unknown')

    statistics = scraper_data.get('statistics')
    stat_list = statistics.get('list') if isinstance(statistics, dict) else None
    rows = pick_stats(stat_list)
    if not rows:
        print(f"[stats] {match_id}: no statistics published — skipping stats page.")
        return None

    competition = competition or match_sample.get('competition_name')

    # Same template as slide one, then blurred: this page is dense with small
    # marks and a sharp stadium photo behind them is unreadable.
    img = load_template(competition, match_id)
    img = img.filter(ImageFilter.GaussianBlur(BG_BLUR))
    img = Image.alpha_composite(img, Image.new('RGBA', img.size, (6, 9, 15, BG_DARKEN)))

    # Crests and logos are pasted straight onto the background; everything else
    # is drawn on this transparent layer, shadowed and composited in one pass.
    ink = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ink)

    home_crest = _load_crest_image(home_team)
    away_crest = _load_crest_image(away_team)
    home_color, away_color = bar_colors(_team_palette(home_team, home_crest),
                                        _team_palette(away_team, away_crest))

    brand_logo = get_brand_logo_url()
    if brand_logo:
        _draw_crest(img, brand_logo, BRAND_LOGO_BOX)
    _draw_centered_text(draw, 'MATCH STATS', TITLE_BOX,
                        FONT_BOLD, TITLE_FONT_MAX, TITLE_FONT_MIN, COLOR_TITLE)

    # ── Header: momentum, or a team strip when the desktop feed had nothing ───
    tendencies = scraper_data.get('tendencies')
    if isinstance(tendencies, dict) and tendencies.get('data'):
        _paste_centred(img, home_crest, HOME_KEY_BOX)
        _paste_centred(img, away_crest, AWAY_KEY_BOX)
        _draw_centered_text(draw, 'MOMENTUM', MOM_LABEL_BOX,
                            FONT_BOLD, 30, 12, COLOR_MUTED)
        draw_momentum(img, draw, tendencies, home_color, away_color,
                      scraper_data.get('events'), raw_home, raw_away)
    else:
        print(f"[stats] {match_id}: no momentum series — using the team strip.")
        draw_team_strip(img, draw, home_team, away_team, home_crest, away_crest)

    # ── Stat rows ─────────────────────────────────────────────────────────────
    row_height = (BLOCK_BOTTOM - BLOCK_TOP) / len(rows)
    width = BLOCK_R - BLOCK_L
    for i, (label, home_value, away_value) in enumerate(rows):
        top = BLOCK_TOP + row_height * i
        label_y = top + row_height * 0.28
        bar_y = top + row_height * 0.70
        bar_height = max(BAR_MIN_PX, int(row_height * 0.19))
        font_size = min(LABEL_FONT_MAX, int(row_height * 0.36))

        home_num, away_num = _num(home_value), _num(away_value)
        home_ink = COLOR_TITLE if home_num >= away_num else COLOR_MUTED
        away_ink = COLOR_TITLE if away_num >= home_num else COLOR_MUTED
        value_font = _font(FONT_BOLD, font_size + VALUE_FONT_BUMP)
        draw.text((BLOCK_L, label_y), home_value, font=value_font,
                  fill=home_ink, anchor='lm')
        draw.text((BLOCK_R, label_y), away_value, font=value_font,
                  fill=away_ink, anchor='rm')
        _draw_text_at_size(draw, label,
                           (BLOCK_L + 120, int(label_y - 24), BLOCK_R - 120, int(label_y + 24)),
                           FONT_BOLD, font_size, COLOR_NAME)

        total = home_num + away_num
        split = BLOCK_L + width * (home_num / total if total else 0.5)
        draw.rounded_rectangle((BLOCK_L, bar_y - bar_height / 2,
                                BLOCK_R, bar_y + bar_height / 2),
                               radius=bar_height / 2, fill=COLOR_TRACK)
        _rounded_bar(draw, BLOCK_L, bar_y - bar_height / 2,
                     split - BAR_GAP_PX, bar_y + bar_height / 2, (*home_color, 255))
        _rounded_bar(draw, split + BAR_GAP_PX, bar_y - bar_height / 2,
                     BLOCK_R, bar_y + bar_height / 2, (*away_color, 255))

    if competition:
        _draw_centered_text(draw, str(competition).upper(), FOOTER_BOX,
                            FONT_BOLD, 32, 10, COLOR_STADIUM)

    # ── Compose: shadow under, ink over ───────────────────────────────────────
    img = Image.alpha_composite(img, _shadow_of(ink))
    img = Image.alpha_composite(img, ink)

    os.makedirs('output', exist_ok=True)
    out = f"output/stats_{match_id}.png"
    img.convert('RGB').save(out, quality=95)
    print(f"[stats] Saved: {out}")
    return out
