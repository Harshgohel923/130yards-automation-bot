# scorecard.py — Pillow template stamper
"""
Reads scraper_data dict (returned by football_scraper_dom.get_match_data)
and stamps it onto a Cloudinary template (or a local fallback).

Coordinate boxes are sourced from coords_log.txt (FT_template.png, 2448x3264 px).
"""

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import glob
import os

from config import (get_crest_url, get_competition_logo_url, get_brand_logo_url,
                    select_template_key,
                    STADIUM_NAME_ALIASES, LOCAL_SYMBOLS, TEAM_NAME_ALIASES)
from cloudinary_utils import LOCAL_CACHE_DIR as TEMPLATE_CACHE_DIR, fetch_template
# Read-only reuse of the overlay card's colour logic so both styles agree.
from overlay_scorebar import TEAM_COLORS, FALLBACK_COLORS, _dominant_colors
from logo_fetch import COMPETITION_DISPLAY, resolve_competition

# ── BOUNDING BOXES (x1, y1, x2, y2) ─────────────────────────────────────────
# Picked with pick_coords.py on the 1086x1448 templates (vertical centre x=543).
EVENT_TITLE_BOX  = (284, 183, 802, 289)     # 'FULL TIME' / 'HALF TIME'
BRAND_LOGO_BOX   = (56, 70, 120, 135)       # 130 Yards mark
COMP_LOGO_BOX    = (474, 307, 612, 445)     # competition logo
GROUP_STAGE_BOX  = (362, 460, 724, 493)     # center-aligned text
HOME_CREST_BOX   = (133, 630, 305, 802)     # centred image paste
AWAY_CREST_BOX   = (781, 630, 953, 802)     # centred image paste
HOME_SCORE_BOX   = (362, 629, 514, 802)     # center-aligned text
AWAY_SCORE_BOX   = (572, 629, 724, 802)     # center-aligned text
HOME_NAME_BOX    = (56, 825, 331, 860)      # center-aligned text
AWAY_NAME_BOX    = (755, 825, 1030, 860)    # center-aligned text
PENALTY_SCORE_BOX = (392, 822, 695, 859)    # center-aligned, sits between the names
HOME_SCORERS_BOX = (54, 889, 514, 1372)     # left-aligned scorer lines
AWAY_SCORERS_BOX = (572, 889, 1032, 1372)   # right-aligned scorer lines
STADIUM_BOX      = (362, 1401, 724, 1431)   # center-aligned text

# ── FONT PATHS ────────────────────────────────────────────────────────────────
FONT_BOLD    = 'assets/fonts/BebasNeue-Regular.ttf'
FONT_REGULAR = 'assets/fonts/BebasNeue-Regular.ttf'

# ── COLOURS ───────────────────────────────────────────────────────────────────
COLOR_SCORE   = (200, 210, 225, 255)
COLOR_SCORER  = (220, 230, 245, 255)
COLOR_MINUTE  = (184, 134, 11, 255)   # minute number color (tweak freely)
COLOR_EXTRA   = (160, 170, 190, 255)
COLOR_NAME    = (200, 210, 225, 255)
COLOR_STAGE   = (200, 210, 225, 255)
COLOR_STADIUM = (200, 210, 225, 255)
COLOR_TITLE   = (255, 255, 255, 255)   # FULL TIME / HALF TIME headline

# ── FONT SIZE LIMITS ──────────────────────────────────────────────────────────
# Sized for the 1086x1448 templates. Each MAX is a ceiling — text is shrunk
# to fit its box — so only raise a MAX if a zone looks smaller than its box.
SCORE_FONT_MAX  = 240
SCORE_FONT_MIN  = 40
NAME_FONT_MAX   = 110
NAME_FONT_MIN   = 12
STAGE_FONT_MAX  = 40
STAGE_FONT_MIN  = 10
TITLE_FONT_MAX  = 180
TITLE_FONT_MIN  = 24
EVENT_TITLE_MAP = {'HT': 'HALF TIME', 'FT': 'FULL TIME'}
SCORER_FONT_MAX  = 40
SCORER_FONT_MIN  = 12
SCORER_LINE_GAP  = 22
SCORER_LINE_GAP_MIN = 6    # tightened before the font is allowed to shrink
# Short scorer lists get breathing room under the team divider; busy ones need
# every pixel, so the drop is skipped above this many lines.
SCORER_DROP          = 25
SCORER_DROP_MAX_LINES = 8
# Busy cards may lift the crest/score/name block into the empty space above,
# which extends the scorer columns upward. Box sizes never change.
MAX_CONTENT_LIFT = 120
LIFT_STEP        = 12
SYMBOL_TEXT_GAP  = 22   # pixels between symbol and the minute/name block
MINUTE_NAME_GAP       = 22   # pixels between minute number and scorer name
SYMBOL_VERTICAL_OFFSET = 7   # positive = nudge symbol down; tweak until flush
STADIUM_FONT_MAX  = 34
STADIUM_FONT_MIN  = 8
PENALTY_FONT_MAX  = 54
PENALTY_FONT_MIN  = 12
COLOR_PENALTY     = (184, 134, 11, 255)   # gold, matching the scorer minutes

# Team-coloured rule between the team name and its scorer list.
DIVIDER_WIDTH_RATIO = 0.8   # of the team-name box width
DIVIDER_THICKNESS   = 3

# Dash between the two score digits.
SCORE_DASH_WIDTH     = 30
SCORE_DASH_THICKNESS = 6
COLOR_SCORE_DASH     = (255, 255, 255, 255)

# Soft drop shadow applied to every drawn element (text, rules, symbols) so
# the card stays readable on pale templates as well as dark ones.
SHADOW_BLUR     = 5
SHADOW_OFFSET   = (0, 3)
SHADOW_STRENGTH = 1.9   # alpha multiplier before blurring; >1 = denser shadow


# ── Scraper round_name → display name ───────────────────────────────────────
ROUND_NAME_MAP = {
    'R':   'GROUP STAGE',
    '32': 'ROUND OF 32',
    '16': 'ROUND OF 16',
    '8':  'QUARTER-FINAL',
    '4':  'SEMI-FINAL',
    '3RD': 'THIRD PLACE',
    '1':   'FINAL',
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _text_size(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _fit_font_to_box(draw, text, box, font_path, max_size, min_size):
    bw = box[2] - box[0]
    bh = box[3] - box[1]
    for size in range(max_size, min_size - 1, -1):
        f = _font(font_path, size)
        tw, th = _text_size(draw, text, f)
        if tw <= bw and th <= bh:
            return size
    return min_size


def _draw_text_at_size(draw, text, box, font_path, size, color):
    """Draw text centred in box at an explicit size (no auto-fit)."""
    font = _font(font_path, size)
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    draw.text((cx, cy), text, font=font, fill=color, anchor='mm')


def _draw_centered_text(draw, text, box, font_path, max_size, min_size, color):
    """Fit text into box and draw it centered both horizontally and vertically."""
    size = _fit_font_to_box(draw, text, box, font_path, max_size, min_size)
    _draw_text_at_size(draw, text, box, font_path, size, color)


def _shift(box, dy):
    """Move a box vertically by dy (negative = up)."""
    if box == (0, 0, 0, 0):
        return box
    return (box[0], box[1] + dy, box[2], box[3] + dy)


def _grow_top(box, dy):
    """Extend a box upward by dy, leaving its bottom edge where it is."""
    if box == (0, 0, 0, 0):
        return box
    return (box[0], box[1] - dy, box[2], box[3])


def _load_symbol(path, height):
    """Load a symbol image resized to the given height, preserving aspect ratio."""
    if not path or not os.path.exists(path):
        return None
    try:
        sym = Image.open(path).convert('RGBA')
        ratio = height / sym.height
        new_w = max(1, int(sym.width * ratio))
        return sym.resize((new_w, height), Image.LANCZOS)
    except Exception:
        return None


def _will_fit(lines, box, font_size, gap=None):
    """Return True if all scorer lines fit inside box (both height and width)."""
    if not lines:
        return True
    if gap is None:
        gap = SCORER_LINE_GAP
    dummy = Image.new('RGBA', (1, 1))
    draw  = ImageDraw.Draw(dummy)
    font  = _font(FONT_REGULAR, font_size)
    _, lh = _text_size(draw, "Ag", font)
    bh = box[3] - box[1]
    bw = box[2] - box[0]
    needed_h = len(lines) * (lh + gap) - gap
    if needed_h > bh:
        return False
    for entry in lines:
        mw, _ = _text_size(draw, entry['minute'], font)
        nw, _ = _text_size(draw, entry['display_name'], font)
        text_w = mw + MINUTE_NAME_GAP + nw
        # symbol is sized to lh (line height), not font_size
        total_w = lh + SYMBOL_TEXT_GAP + text_w
        if total_w > bw:
            return False
    return True


def _fit_scorers(home_lines, away_lines, home_box, away_box):
    """
    Choose how to make both scorer lists fit, in the order that costs the
    least legibility:

      1. keep the font large and tighten the line spacing
      2. still tight? lift the whole result block into the empty space above,
         which extends the scorer columns upward (box sizes unchanged)
      3. only then shrink the font

    Returns (font_size, line_gap, lift).
    """
    lifts = list(range(0, MAX_CONTENT_LIFT + 1, LIFT_STEP))
    for size in range(SCORER_FONT_MAX, SCORER_FONT_MIN - 1, -1):
        for lift in lifts:
            hb, ab = _grow_top(home_box, lift), _grow_top(away_box, lift)
            for gap in range(SCORER_LINE_GAP, SCORER_LINE_GAP_MIN - 1, -1):
                if (_will_fit(home_lines, hb, size, gap) and
                        _will_fit(away_lines, ab, size, gap)):
                    return size, gap, lift
    # Nothing fits even at the floor — smallest font, tightest spacing, full
    # lift; _draw_scorer_lines then truncates with "& N more".
    return SCORER_FONT_MIN, SCORER_LINE_GAP_MIN, MAX_CONTENT_LIFT


# ── Data extraction helpers ───────────────────────────────────────────────────

def _goal_symbol_path(event_type: str) -> str:
    """Map scraper event type string to a local symbol image path via LOCAL_SYMBOLS."""
    mapping = {
        'penalty_goal':   'penalty_goal',
        'own_goal':       'own_goal',
        'penalty_missed': 'penalty_missed',
        'red_card':       'red_card',
    }
    key = mapping.get(event_type.lower(), 'normal_goal')
    return LOCAL_SYMBOLS.get(key, '')


def _extract_scorer_lines(events: list, team_name: str) -> list[dict]:
    """
    Pull displayable events for team_name from the scraper events list.

    Each entry is:
      { 'display_name': str, 'minute': str, 'symbol_path': str }

    Included types: goal, penalty_goal, own_goal, yellow_card, red_card,
                    penalty_missed.
    Substitutions and half-time markers are intentionally excluded from the
    scorecard display.
    """
    DISPLAY_TYPES = {
        'goal', 'penalty_goal', 'own_goal',
        'red_card', 'penalty_missed',
    }
    lines = []
    if not isinstance(events, list):
        return lines

    for ev in events:
        ev_type = ev.get('type', '')
        if ev_type not in DISPLAY_TYPES:
            continue
        if ev.get('team') != team_name:
            continue

        player = ev.get('player', '') or '?'
        minute = ev.get('minute', '?')

        lines.append({
            'display_name': player,
            'minute':       minute,
            'symbol_path':  _goal_symbol_path(ev_type),
        })
    return lines


def _parse_scores(match_sample: dict) -> tuple[str, str]:
    """
    Returns (home_score, away_score) as strings.
    Uses full-time score (fs_A / fs_B); falls back to half-time (hts_A / hts_B).
    """
    home = str(match_sample.get('fs_A') or match_sample.get('hts_A') or '0')
    away = str(match_sample.get('fs_B') or match_sample.get('hts_B') or '0')
    return home, away


# ── Crest renderer ────────────────────────────────────────────────────────────

def _draw_crest(img, crest_url_or_path: str | None, box):
    """
    Download (or load) a team crest and paste it centred inside box.

    Returns the loaded crest image so the caller can sample its colours for
    the team divider; None when the crest could not be loaded.
    """
    if not crest_url_or_path:
        return None
    try:
        if crest_url_or_path.startswith('http'):
            import requests, io
            r = requests.get(crest_url_or_path, timeout=8)
            r.raise_for_status()
            crest = Image.open(io.BytesIO(r.content)).convert('RGBA')
        else:
            crest = Image.open(crest_url_or_path).convert('RGBA')
    except Exception as e:
        print(f"[scorecard] Could not load crest: {e}")
        return None

    bw = box[2] - box[0]
    bh = box[3] - box[1]
    crest.thumbnail((bw, bh), Image.LANCZOS)
    cx = box[0] + (bw - crest.width)  // 2
    cy = box[1] + (bh - crest.height) // 2
    img.paste(crest, (cx, cy), crest)
    return crest


def _shadow_of(layer):
    """
    Build a soft drop shadow from everything drawn on `layer`.

    Takes the layer's alpha as the shadow silhouette, darkens it, blurs it and
    offsets it — so one call shadows every glyph, rule and symbol at once,
    instead of each draw site handling its own.
    """
    alpha = layer.split()[3].point(lambda v: min(255, int(v * SHADOW_STRENGTH)))
    shadow = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))

    offset = Image.new('RGBA', layer.size, (0, 0, 0, 0))
    offset.paste(shadow, SHADOW_OFFSET)
    return offset


def _draw_score_dash(draw, home_box, away_box):
    """Short white dash centred in the gap between the two score digits."""
    if home_box == (0, 0, 0, 0) or away_box == (0, 0, 0, 0):
        return
    cx = (home_box[2] + away_box[0]) / 2
    cy = (home_box[1] + home_box[3] + away_box[1] + away_box[3]) / 4
    draw.line((cx - SCORE_DASH_WIDTH / 2, cy, cx + SCORE_DASH_WIDTH / 2, cy),
              fill=COLOR_SCORE_DASH, width=SCORE_DASH_THICKNESS)


def _team_colors(team_name: str, crest):
    """
    Two brand colours for a team — curated where we have them, otherwise
    sampled from the crest. Reuses the overlay card's logic so both card
    styles derive identical colours for the same team.
    """
    curated = TEAM_COLORS.get(team_name)
    if curated:
        return curated
    if crest is None:
        return FALLBACK_COLORS
    try:
        return _dominant_colors(crest)
    except Exception:
        return FALLBACK_COLORS


def _draw_team_divider(draw, name_box, scorers_box, colors):
    """
    Horizontal rule between a team's name and its scorer list, split into one
    segment per brand colour — the static counterpart of the overlay card's
    partition line. Sits midway between the two boxes and is centred on the
    name, so it follows whatever coordinates pick_coords.py produced.
    """
    if name_box == (0, 0, 0, 0) or scorers_box == (0, 0, 0, 0) or not colors:
        return
    cx = (name_box[0] + name_box[2]) / 2
    y  = (name_box[3] + scorers_box[1]) / 2
    width = (name_box[2] - name_box[0]) * DIVIDER_WIDTH_RATIO
    seg = width / len(colors)
    x0 = cx - width / 2
    for i, col in enumerate(colors):
        draw.line((x0 + i * seg, y, x0 + (i + 1) * seg, y),
                  fill=(*col[:3], 255), width=DIVIDER_THICKNESS)


# ── Scorer-line renderer ──────────────────────────────────────────────────────

def _draw_scorer_lines(img, draw, lines, box, align, font_size, gap=None):
    """
    Renders scorer lines inside box.

    Layout per line:
      HOME (left-aligned):  [symbol] [gap] [minute] [gap] [name]
      AWAY (right-aligned): [name] [gap] [minute] [gap] [symbol]

    Symbol is sized to match the text line height so they sit on the same baseline.
    Minute drawn in COLOR_MINUTE, scorer name in COLOR_SCORER.

    align: 'left' (home) or 'right' (away)
    """
    x1, y1, x2, y2 = box
    font = _font(FONT_REGULAR, font_size)

    _, lh = _text_size(draw, "Ag", font)
    step  = lh + (SCORER_LINE_GAP if gap is None else gap)
    # Symbol is exactly as tall as the text line so they are vertically flush
    sym_h = lh

    # Decide which lines are visible; reserve a slot for "& N more" if needed
    visible = []
    hidden  = 0
    for entry in lines:
        needed_bot = y1 + len(visible) * step + lh
        remaining  = len(lines) - len(visible) - 1
        if needed_bot + (step if remaining > 0 else 0) > y2:
            hidden += 1
        else:
            visible.append(entry)

    for i, entry in enumerate(visible):
        cy      = y1 + i * step
        minute  = entry['minute']
        name    = entry['display_name']

        sym_img = _load_symbol(entry['symbol_path'], sym_h)
        sw      = sym_img.width if sym_img else 0

        mw, _ = _text_size(draw, minute, font)
        nw, _ = _text_size(draw, name, font)

        # Truncate name if the full block is too wide
        max_text_w = (x2 - x1) - sw - (SYMBOL_TEXT_GAP if sw else 0)
        full_w = mw + MINUTE_NAME_GAP + nw
        if full_w > max_text_w:
            while len(name) > 1:
                name = name[:-1]
                nw, _ = _text_size(draw, name + '…', font)
                if mw + MINUTE_NAME_GAP + nw <= max_text_w:
                    name = name + '…'
                    nw, _ = _text_size(draw, name, font)
                    break

        if align == 'left':
            # [symbol] [gap] [minute] [gap] [name]
            sym_x    = x1
            minute_x = x1 + sw + (SYMBOL_TEXT_GAP if sw else 0)
            name_x   = minute_x + mw + MINUTE_NAME_GAP
        else:
            # [name] [gap] [minute] [gap] [symbol]
            sym_x    = x2 - sw
            minute_x = x2 - sw - (SYMBOL_TEXT_GAP if sw else 0) - mw
            name_x   = minute_x - MINUTE_NAME_GAP - nw
            name_x   = max(x1, name_x)

        draw.text((minute_x, cy), minute, font=font, fill=COLOR_MINUTE)
        draw.text((name_x,   cy), name,   font=font, fill=COLOR_SCORER)

        if sym_img:
            paste_x = max(x1, min(sym_x, x2 - sw))
            img.paste(sym_img, (paste_x, cy + SYMBOL_VERTICAL_OFFSET), sym_img)

    if hidden > 0:
        more_str = f"& {hidden} more"
        fy    = y1 + len(visible) * step
        mw, _ = _text_size(draw, more_str, font)
        mx    = x1 if align == 'left' else (x2 - mw)
        draw.text((mx, fy), more_str, font=font, fill=COLOR_EXTRA)


# ── Main public function ──────────────────────────────────────────────────────

def generate_scorecard(scraper_data: dict, event_type: str = 'FT', match_id_override: str = '',
                       home_name: str | None = None, away_name: str | None = None,
                       competition: str | None = None) -> str:
    """
    Build a scorecard image from scraper_data (output of get_match_data).
    event_type: 'HT' or 'FT' — controls which template is fetched and which
                score values are used.
    Returns the local path of the saved PNG.
    """
    match_sample  = scraper_data.get('matchSample', {})
    raw_home_team = match_sample.get('team_A_name', 'Home')
    raw_away_team = match_sample.get('team_B_name', 'Away')
    # Display / crest names: the validated matches.json names win over the
    # scraper's spelling; raw names stay for event matching only.
    home_team     = home_name or TEAM_NAME_ALIASES.get(raw_home_team, raw_home_team)
    away_team     = away_name or TEAM_NAME_ALIASES.get(raw_away_team, raw_away_team)
    match_id      = str(match_sample.get('match_id') or match_id_override or 'unknown')
    events        = scraper_data.get('events', [])

    # ── Scores ────────────────────────────────────────────────────────────────
    ps_home_raw = str(match_sample.get('ps_A') or '').strip()
    ps_away_raw = str(match_sample.get('ps_B') or '').strip()

    if event_type == 'HT':
        home_score = str(match_sample.get('hts_A') or '0')
        away_score = str(match_sample.get('hts_B') or '0')
    else:
        home_score, away_score = _parse_scores(match_sample)
        if ps_home_raw and ps_away_raw:
            try:
                home_score = str(int(home_score) - int(ps_home_raw))
                away_score = str(int(away_score) - int(ps_away_raw))
            except ValueError:
                pass

    # ── Template ──────────────────────────────────────────────────────────────
    # UCL matches get the dedicated design, everything else a random one —
    # seeded by match_id so HT, FT and any correction share a look.
    template_key = select_template_key(
        competition or match_sample.get('competition_name'), match_id)
    template_path = None
    try:
        template_path = fetch_template(template_key)
    except Exception as e:
        print(f"[scorecard] Cloudinary template fetch failed: {e}")

    if not template_path or not os.path.exists(template_path):
        # Any previously cached template beats failing the whole card.
        cached = sorted(glob.glob(os.path.join(TEMPLATE_CACHE_DIR, '*.png')))
        if not cached:
            raise RuntimeError(
                f"No template available for '{template_key}' — Cloudinary "
                f"fetch failed and no cached template exists.")
        template_path = cached[0]
        print(f"[scorecard] Falling back to cached template: {template_path}")

    print(f"[scorecard] Template: {template_key}")
    img = Image.open(template_path).convert('RGBA')

    # Crests and logos are pasted straight onto the template; everything else
    # (text, rules, scorer symbols) is drawn on this transparent layer, which
    # is shadowed and composited in one pass at the end.
    ink  = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ink)

    # ── Competition label — center aligned ────────────────────────────────────
    # Competition name only, never the round: matches.json / the scraper always
    # carry a competition, whereas round data is patchy and reads as clutter.
    comp_raw = (competition or match_sample.get('competition_name') or '')
    comp_key = resolve_competition(comp_raw)
    stage_text = COMPETITION_DISPLAY.get(comp_key) or comp_raw.strip()
    if stage_text and GROUP_STAGE_BOX != (0, 0, 0, 0):
        _draw_centered_text(draw, stage_text.upper(), GROUP_STAGE_BOX,
                            FONT_BOLD, STAGE_FONT_MAX, STAGE_FONT_MIN, COLOR_STAGE)

    # ── 'FULL TIME' / 'HALF TIME' headline ────────────────────────────────────
    # Templates that bake the headline into the artwork leave EVENT_TITLE_BOX
    # unset; templates that don't get it drawn here.
    if EVENT_TITLE_BOX != (0, 0, 0, 0):
        title_text = EVENT_TITLE_MAP.get(event_type.upper(), event_type.upper())
        _draw_centered_text(draw, title_text, EVENT_TITLE_BOX,
                            FONT_BOLD, TITLE_FONT_MAX, TITLE_FONT_MIN, COLOR_TITLE)

    # ── Scorer lines: decide the fit before anything is positioned ────────────
    # Use raw (un-normalized) names because event['team'] reflects the scraper value.
    # When match goes to penalties, exclude 120' shootout events from display.
    # Only penalty kicks are shootout events — open-play/own goals at 120'
    # (e.g. scored in ET injury time) must still be shown.
    filtered_events = events
    if ps_home_raw and ps_away_raw:
        filtered_events = [e for e in events
                           if not (e.get('minute') == "120'"
                                   and e.get('type') in ('penalty_goal', 'penalty_missed'))]
    home_lines = _extract_scorer_lines(filtered_events, raw_home_team)
    away_lines = _extract_scorer_lines(filtered_events, raw_away_team)

    # Short lists sit a little lower so they don't crowd the team divider.
    # Busy lists keep every pixel they have.
    drop = (SCORER_DROP
            if max(len(home_lines), len(away_lines)) <= SCORER_DROP_MAX_LINES
            else 0)
    chosen_size, chosen_gap, lift = _fit_scorers(
        home_lines, away_lines,
        _grow_top(HOME_SCORERS_BOX, -drop), _grow_top(AWAY_SCORERS_BOX, -drop))
    if lift:
        print(f"[scorecard] Busy card: lifting result block {lift}px, "
              f"scorer gap {chosen_gap}px, font {chosen_size}px")

    # A lift moves the crest/score/name block up and extends the scorer
    # columns upward by the same amount; every box keeps its size.
    home_crest_box   = _shift(HOME_CREST_BOX, -lift)
    away_crest_box   = _shift(AWAY_CREST_BOX, -lift)
    home_score_box   = _shift(HOME_SCORE_BOX, -lift)
    away_score_box   = _shift(AWAY_SCORE_BOX, -lift)
    home_name_box    = _shift(HOME_NAME_BOX, -lift)
    away_name_box    = _shift(AWAY_NAME_BOX, -lift)
    penalty_box      = _shift(PENALTY_SCORE_BOX, -lift)
    # The divider stays anchored under the team name (lift only); the scorer
    # text additionally takes the drop.
    home_divider_box = _grow_top(HOME_SCORERS_BOX, lift)
    away_divider_box = _grow_top(AWAY_SCORERS_BOX, lift)
    home_scorers_box = _grow_top(HOME_SCORERS_BOX, lift - drop)
    away_scorers_box = _grow_top(AWAY_SCORERS_BOX, lift - drop)

    # ── Team crests — centred inside their boxes ───────────────────────────────
    home_crest = get_crest_url(home_team)
    away_crest = get_crest_url(away_team)

    # Competition logo (brand logo for friendlies/unknowns). Skipped until
    # COMP_LOGO_BOX is set on the logo-free template.
    if COMP_LOGO_BOX != (0, 0, 0, 0):
        comp_url = get_competition_logo_url(
            competition or match_sample.get('competition_name'))
        if comp_url:
            _draw_crest(img, comp_url, COMP_LOGO_BOX)

    # 130 Yards mark — templates that bake it in leave BRAND_LOGO_BOX unset.
    if BRAND_LOGO_BOX != (0, 0, 0, 0):
        _draw_crest(img, get_brand_logo_url(), BRAND_LOGO_BOX)
    home_crest_img = away_crest_img = None
    if home_crest_box != (0, 0, 0, 0):
        home_crest_img = _draw_crest(img, home_crest, home_crest_box)
    if away_crest_box != (0, 0, 0, 0):
        away_crest_img = _draw_crest(img, away_crest, away_crest_box)

    # ── Team names — center aligned ───────────────────────────────────────────
    if home_name_box != (0, 0, 0, 0) and home_team:
        _draw_centered_text(draw, home_team, home_name_box,
                            FONT_BOLD, NAME_FONT_MAX, NAME_FONT_MIN, COLOR_NAME)
    if away_name_box != (0, 0, 0, 0) and away_team:
        _draw_centered_text(draw, away_team, away_name_box,
                            FONT_BOLD, NAME_FONT_MAX, NAME_FONT_MIN, COLOR_NAME)

    # ── Team-coloured divider between each name and its scorer list ───────────
    _draw_team_divider(draw, home_name_box, home_divider_box,
                       _team_colors(home_team, home_crest_img))
    _draw_team_divider(draw, away_name_box, away_divider_box,
                       _team_colors(away_team, away_crest_img))

    # ── Scores — center aligned, both sides at one size ───────────────────────
    # Fit each independently, then use the smaller: a lopsided '20 - 0' must
    # not render the '0' larger than the '20'.
    score_size = min(
        _fit_font_to_box(draw, home_score, home_score_box,
                         FONT_BOLD, SCORE_FONT_MAX, SCORE_FONT_MIN),
        _fit_font_to_box(draw, away_score, away_score_box,
                         FONT_BOLD, SCORE_FONT_MAX, SCORE_FONT_MIN),
    )
    for score_val, box in [(home_score, home_score_box), (away_score, away_score_box)]:
        if box != (0, 0, 0, 0):
            _draw_text_at_size(draw, score_val, box, FONT_BOLD, score_size, COLOR_SCORE)
    _draw_score_dash(draw, home_score_box, away_score_box)

    # ── Penalty shootout score — only shown when match went to penalties ───────
    ps_home = ps_home_raw if event_type != 'HT' else ''
    ps_away = ps_away_raw if event_type != 'HT' else ''
    if ps_home and ps_away and penalty_box != (0, 0, 0, 0):
        # Gold text, no backing plate — the card-wide shadow carries it.
        penalty_text = f"PENALTIES: {ps_home}-{ps_away}"
        _draw_centered_text(draw, penalty_text, penalty_box,
                            FONT_BOLD, PENALTY_FONT_MAX, PENALTY_FONT_MIN, COLOR_PENALTY)

    # ── Stadium — center aligned ──────────────────────────────────────────────
    raw_stadium = (scraper_data.get('matchFormation') or {}).get('venue_name') or ''
    stadium = STADIUM_NAME_ALIASES.get(raw_stadium, raw_stadium)
    if stadium and STADIUM_BOX != (0, 0, 0, 0):
        _draw_centered_text(draw, stadium, STADIUM_BOX,
                            FONT_REGULAR, STADIUM_FONT_MAX, STADIUM_FONT_MIN, COLOR_STADIUM)

    # Home: left-aligned  |  Away: right-aligned — symbols land on `ink` too,
    # so they pick up the same shadow as the text.
    _draw_scorer_lines(ink, draw, home_lines, home_scorers_box, 'left',
                       chosen_size, chosen_gap)
    _draw_scorer_lines(ink, draw, away_lines, away_scorers_box, 'right',
                       chosen_size, chosen_gap)

    # ── Compose: shadow under, ink over ───────────────────────────────────────
    img = Image.alpha_composite(img, _shadow_of(ink))
    img = Image.alpha_composite(img, ink)

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs('output', exist_ok=True)
    out = f"output/scorecard_{match_id}_{event_type}.png"
    img.convert('RGB').save(out, quality=95)
    print(f"[scorecard] Saved: {out}")
    return out