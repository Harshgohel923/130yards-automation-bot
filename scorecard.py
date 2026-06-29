# scorecard.py — Pillow template stamper
"""
Reads scraper_data dict (returned by football_scraper_dom.get_match_data)
and stamps it onto a Cloudinary template (or a local fallback).

Coordinate boxes are sourced from coords_log.txt (FT_template.png, 2448x3264 px).
"""

from PIL import Image, ImageDraw, ImageFont
import os

from config import get_crest_url, STADIUM_NAME_ALIASES, LOCAL_SYMBOLS, TEAM_NAME_ALIASES
from cloudinary_utils import fetch_template

# ── BOUNDING BOXES (x1, y1, x2, y2) ─────────────────────────────────────────
GROUP_STAGE_BOX  = (721, 1106, 1697, 1193)   # center-aligned text
HOME_CREST_BOX   = (228, 1588, 707, 2027)   # centred image paste
AWAY_CREST_BOX   = (1762, 1584, 2241, 2023)   # centred image paste
HOME_SCORE_BOX   = (747, 1584, 1120, 2027)   # center-aligned text
AWAY_SCORE_BOX   = (1320, 1577, 1722, 2023)   # center-aligned text
HOME_NAME_BOX    = (94, 2074, 884, 2165)   # center-aligned text
AWAY_NAME_BOX    = (1573, 2070, 2368, 2157)   # center-aligned text
STADIUM_BOX      = (772, 3147, 1646, 3227)   # center-aligned text
HOME_SCORERS_BOX = (83, 2212, 1120, 3100)   # left-aligned scorer lines
AWAY_SCORERS_BOX = (1316, 2197, 2368, 3100)   # right-aligned scorer lines

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

# ── FONT SIZE LIMITS ──────────────────────────────────────────────────────────
SCORE_FONT_MAX  = 500
SCORE_FONT_MIN  = 500
NAME_FONT_MAX   = 250
NAME_FONT_MIN   = 18
STAGE_FONT_MAX  = 80
STAGE_FONT_MIN  = 12
SCORER_FONT_MAX  = 84
SCORER_FONT_MIN  = 18
SCORER_LINE_GAP  = 50
SYMBOL_TEXT_GAP  = 50   # pixels between symbol and the minute/name block
MINUTE_NAME_GAP       = 50   # pixels between minute number and scorer name
SYMBOL_VERTICAL_OFFSET = 15  # positive = nudge symbol down; tweak until flush
STADIUM_FONT_MAX = 72
STADIUM_FONT_MIN = 10


# ── Round-code → display name ─────────────────────────────────────────────────
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


def _draw_centered_text(draw, text, box, font_path, max_size, min_size, color):
    """Fit text into box and draw it centered both horizontally and vertically."""
    size = _fit_font_to_box(draw, text, box, font_path, max_size, min_size)
    font = _font(font_path, size)
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    draw.text((cx, cy), text, font=font, fill=color, anchor='mm')


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


def _will_fit(lines, box, font_size):
    """Return True if all scorer lines fit inside box (both height and width) at font_size."""
    if not lines:
        return True
    dummy = Image.new('RGBA', (1, 1))
    draw  = ImageDraw.Draw(dummy)
    font  = _font(FONT_REGULAR, font_size)
    _, lh = _text_size(draw, "Ag", font)
    bh = box[3] - box[1]
    bw = box[2] - box[0]
    needed_h = len(lines) * (lh + SCORER_LINE_GAP) - SCORER_LINE_GAP
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
    """Download (or load) a team crest and paste it centred inside box."""
    if not crest_url_or_path:
        return
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
        return

    bw = box[2] - box[0]
    bh = box[3] - box[1]
    crest.thumbnail((bw, bh), Image.LANCZOS)
    cx = box[0] + (bw - crest.width)  // 2
    cy = box[1] + (bh - crest.height) // 2
    img.paste(crest, (cx, cy), crest)


# ── Scorer-line renderer ──────────────────────────────────────────────────────

def _draw_scorer_lines(img, draw, lines, box, align, font_size):
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
    step  = lh + SCORER_LINE_GAP
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

def generate_scorecard(scraper_data: dict, event_type: str = 'FT', match_id_override: str = '') -> str:
    """
    Build a scorecard image from scraper_data (output of get_match_data).
    event_type: 'HT' or 'FT' — controls which template is fetched and which
                score values are used.
    Returns the local path of the saved PNG.
    """
    match_sample  = scraper_data.get('matchSample', {})
    raw_home_team = match_sample.get('team_A_name', 'Home')
    raw_away_team = match_sample.get('team_B_name', 'Away')
    # Normalize for display / crest lookup; keep raw names for event matching
    home_team     = TEAM_NAME_ALIASES.get(raw_home_team, raw_home_team)
    away_team     = TEAM_NAME_ALIASES.get(raw_away_team, raw_away_team)
    match_id      = str(match_sample.get('match_id') or match_id_override or 'unknown')
    events        = scraper_data.get('events', [])

    # ── Scores ────────────────────────────────────────────────────────────────
    if event_type == 'HT':
        home_score = str(match_sample.get('hts_A') or '0')
        away_score = str(match_sample.get('hts_B') or '0')
    else:
        home_score, away_score = _parse_scores(match_sample)

    # ── Template ──────────────────────────────────────────────────────────────
    template_path = None
    try:
        template_path = fetch_template(event_type)
    except Exception as e:
        print(f"[scorecard] Cloudinary template fetch failed: {e}")

    fallback_template = (
        f'assets/{event_type.lower()}_template.png'
        if os.path.exists(f'assets/{event_type.lower()}_template.png')
        else 'assets/template.png'
    )
    if not template_path or not os.path.exists(template_path):
        template_path = fallback_template

    img  = Image.open(template_path).convert('RGBA')
    draw = ImageDraw.Draw(img)

    # ── Group / Stage label — center aligned ──────────────────────────────────
    group_name = (match_sample.get('group_name') or '').strip()
    round_code = (match_sample.get('round_name') or '').strip()
    round_label = ROUND_NAME_MAP.get(round_code, round_code)
    if group_name and round_label:
        stage_text = f"{group_name.upper()} | {round_label}"
    elif group_name:
        stage_text = group_name.upper()
    elif round_label:
        stage_text = round_label
    else:
        stage_text = ''
    if stage_text and GROUP_STAGE_BOX != (0, 0, 0, 0):
        _draw_centered_text(draw, stage_text, GROUP_STAGE_BOX,
                            FONT_BOLD, STAGE_FONT_MAX, STAGE_FONT_MIN, COLOR_STAGE)

    # ── Team crests — centred inside their boxes ───────────────────────────────
    home_crest = get_crest_url(home_team)
    away_crest = get_crest_url(away_team)
    if HOME_CREST_BOX != (0, 0, 0, 0):
        _draw_crest(img, home_crest, HOME_CREST_BOX)
    if AWAY_CREST_BOX != (0, 0, 0, 0):
        _draw_crest(img, away_crest, AWAY_CREST_BOX)

    # ── Team names — center aligned ───────────────────────────────────────────
    if HOME_NAME_BOX != (0, 0, 0, 0) and home_team:
        _draw_centered_text(draw, home_team, HOME_NAME_BOX,
                            FONT_BOLD, NAME_FONT_MAX, NAME_FONT_MIN, COLOR_NAME)
    if AWAY_NAME_BOX != (0, 0, 0, 0) and away_team:
        _draw_centered_text(draw, away_team, AWAY_NAME_BOX,
                            FONT_BOLD, NAME_FONT_MAX, NAME_FONT_MIN, COLOR_NAME)

    # ── Scores — center aligned ───────────────────────────────────────────────
    for score_val, box in [(home_score, HOME_SCORE_BOX), (away_score, AWAY_SCORE_BOX)]:
        if box != (0, 0, 0, 0):
            _draw_centered_text(draw, score_val, box,
                                FONT_BOLD, SCORE_FONT_MAX, SCORE_FONT_MIN, COLOR_SCORE)

    # ── Scorer lines ──────────────────────────────────────────────────────────
    # Use raw (un-normalized) names because event['team'] reflects the scraper value
    home_lines = _extract_scorer_lines(events, raw_home_team)
    away_lines = _extract_scorer_lines(events, raw_away_team)

    # Find the largest font size where BOTH sides fit
    chosen_size = SCORER_FONT_MIN
    for font_size in range(SCORER_FONT_MAX, SCORER_FONT_MIN - 1, -1):
        if (_will_fit(home_lines, HOME_SCORERS_BOX, font_size) and
                _will_fit(away_lines, AWAY_SCORERS_BOX, font_size)):
            chosen_size = font_size
            break

    # ── Stadium — center aligned ──────────────────────────────────────────────
    raw_stadium = (scraper_data.get('matchFormation') or {}).get('venue_name') or ''
    stadium = STADIUM_NAME_ALIASES.get(raw_stadium, raw_stadium)
    if stadium and STADIUM_BOX != (0, 0, 0, 0):
        _draw_centered_text(draw, stadium, STADIUM_BOX,
                            FONT_REGULAR, STADIUM_FONT_MAX, STADIUM_FONT_MIN, COLOR_STADIUM)

    # Home: left-aligned  |  Away: right-aligned
    _draw_scorer_lines(img, draw, home_lines, HOME_SCORERS_BOX, 'left',  chosen_size)
    _draw_scorer_lines(img, draw, away_lines, AWAY_SCORERS_BOX, 'right', chosen_size)

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs('output', exist_ok=True)
    out = f"output/scorecard_{match_id}_{event_type}.png"
    img.convert('RGB').save(out, quality=95)
    print(f"[scorecard] Saved: {out}")
    return out