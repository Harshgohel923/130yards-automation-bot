# lineup_card.py — the pre-match starting XI page
"""
One page per team, posted as a two-slide carousel before kickoff.

The scraper publishes each starting player with a grid position rather than
pixel coordinates: `position_x` is the band ('GK', 'D1', 'DM', 'M', 'AM', 'A')
and `position_y` the side within it ('L', 'CL', 'C', 'CR', 'R'). This module
turns that grid into a pitch drawn in perspective — bands become rows from the
near goal up, sides become columns across the width — so any shape the feed
reports renders without a per-formation template.

Design notes
────────────
  * The layout follows the reference team-sheet card
    (football-lineup-example.png) — crests either side of VS with STARTING XI
    beneath, BENCH and REFEREE down the left, the XI as numbered discs with
    fixed-width name plates, and the brand strip along the bottom — re-laid
    on the 3:4 canvas the scorecard and the stats page already use, so a
    lineup slide and a scorecard slide are the same shape in the feed. The
    reference's own proportions were kept: everything is the width it was on
    the square, and the extra height went into the header's breathing room,
    a deeper pitch and a longer bench column.
  * Each player is a numbered disc — white circle, ring and number in the
    team's own colour — with the name on a colour plate beneath it. The
    team's colour is curated in `overlay_scorebar.TEAM_COLORS`, sampled from
    the crest otherwise, exactly as the scorer underlines and the stats bars
    do. A colour too pale to read on the white disc falls through to the
    team's second colour, then to a darkened version of itself.
  * Same template background as the scorecard and the stats page (via
    `scorecard.load_template`, seeded by match_id, so every slide of a match
    shares a look), blurred and veiled dark the way the reference's stadium
    is.
  * The pitch is a trapezoid, and every row is spread across the width *at
    its own depth*, so the far rows narrow the way they do on television.
  * Columns are placed nearly evenly with a slight pull toward their nominal
    side — the reference's own back four sits at 0.11/0.35/0.63/0.87 of the
    row, an even spread nudged outward.
  * `generate_lineup_card` returns None when that side has no XI published
    yet. Lineups appear roughly an hour before kickoff, so a worker that
    starts early simply asks again on its next poll.
"""

import os

from PIL import Image, ImageDraw, ImageFilter

from config import TEAM_NAME_ALIASES, get_brand_logo_url, get_crest_url
from overlay_scorebar import FALLBACK_COLORS, TEAM_COLORS, _dominant_colors
from scorecard import (COLOR_TITLE, FONT_BOLD, _draw_centered_text,
                       _fit_font_to_box, _font, _shadow_of, load_template)

# ── CANVAS ───────────────────────────────────────────────────────────────────
# 3:4, the shape of the shared 1086x1448 template — the same canvas the
# scorecard and the stats page are drawn on, used whole rather than cropped.
CANVAS_W, CANVAS_H = 1086, 1448

# ── BOUNDING BOXES (x1, y1, x2, y2) ─────────────────────────────────────────
# The header and pitch share one axis at x=612 — the bench column takes the
# left edge, so the pitch (and everything above it) sits right of centre.
HOME_CREST_BOX = (332, 66, 492, 226)       # home always left of the VS…
AWAY_CREST_BOX = (732, 66, 892, 226)       # …away always right, on both slides
VS_BOX         = (532, 92, 692, 196)       # 'VS'
TITLE_BOX      = (472, 228, 752, 268)      # 'STARTING XI'

# The bench column, and the referee beneath it.
COL_L, COL_R      = 44, 292
BENCH_LABEL_BOX   = (COL_L, 300, COL_R, 356)   # 'BENCH'
BENCH_TOP         = 382                    # first name's band starts here
BENCH_LINE_H      = 62                     # the column's rhythm, at its roomiest
BENCH_NAMES_MAX_Y = 1040                   # the rhythm compresses past here instead
BENCH_PLATE_W     = 190                    # one plate width for the whole column
REF_GAP           = 56                     # last bench name → 'REFEREE'
REF_LABEL_H       = 48
REF_NAME_GAP      = 10

# The pitch, as a trapezoid: narrower at the far end. Taller than it was on
# the square — the 3:4 canvas' extra height is mostly pitch, which is also
# what a real pitch seen end-on looks like.
PITCH_TOP, PITCH_BOTTOM = 308, 1278
PITCH_CX                = 612
PITCH_TOP_HALF_W        = 268
PITCH_BOTTOM_HALF_W     = 402

FOOTER_CY       = 1372                     # centreline of the brand strip
FOOTER_LOGO_H   = 52
FOOTER_GAP      = 20                       # between the wordmark and the logo

# ── COLOURS ──────────────────────────────────────────────────────────────────
COLOR_NAME       = (240, 244, 250, 255)
COLOR_LINES      = (255, 255, 255, 235)    # pitch markings
COLOR_BENCH      = (245, 247, 250, 255)    # bench names — white, as the reference
COLOR_DISC       = (255, 255, 255, 255)    # the player disc
COLOR_SIDE_VEIL  = (6, 9, 15, 150)         # darkest point of the left gradient

# ── BACKGROUND ───────────────────────────────────────────────────────────────
BG_BLUR    = 16    # same treatment as the stats page, for the same reason
BG_DARKEN  = 138   # alpha of the flat veil — the reference is nearly night
VEIL_WIDTH = 420   # how far the left gradient reaches before it is gone

# ── FONT SIZE LIMITS ─────────────────────────────────────────────────────────
VS_FONT_MAX     = 112
TITLE_FONT_MAX  = 32
TITLE_FONT_MIN  = 16
BENCH_HEAD_MAX  = 54
BENCH_FONT_MAX  = 30
BENCH_FONT_MIN  = 17
PLAYER_FONT_MAX = 28
PLAYER_FONT_MIN = 18
FOOTER_FONT_MAX = 44

# ── PITCH GEOMETRY ───────────────────────────────────────────────────────────
# Depths as fractions of the pitch height, widths as fractions of the pitch's
# own width at that depth — which is what keeps them square in perspective.
LINE_WIDTH         = 4
HALFWAY_DEPTH      = 0.357  # the reference's halfway line, behind the mid row
CENTRE_CIRCLE_W    = 0.28
CENTRE_CIRCLE_H    = 0.17
PENALTY_W          = 0.55
PENALTY_DEPTH_FAR  = 0.165
PENALTY_DEPTH_NEAR = 0.165
SIX_YARD_W         = 0.28
SIX_YARD_FAR       = 0.075
SIX_YARD_NEAR      = 0.075
GOAL_W             = 0.15
GOAL_DEPTH         = 0.018

# ── PLAYER GRID ──────────────────────────────────────────────────────────────
# Bands the scraper publishes, ordered from a team's own goal forward. Anything
# unrecognised is drawn ahead of the strikers rather than dropped — a band we
# have never seen is still a player who is playing.
ROW_ORDER = ('GK', 'D1', 'D2', 'D', 'DM', 'M', 'AM', 'A', 'F')
# Where a side sits across the pitch, and the order sides run left to right.
SLOT_ANCHOR = {'L': 0.09, 'CL': 0.30, 'C': 0.50, 'CR': 0.70, 'R': 0.91}
SLOT_ORDER  = {'L': 0, 'CL': 1, 'C': 2, 'CR': 3, 'R': 4}
ANCHOR_PULL = 0.15  # the reference back four: an even spread, nudged outward

ROW_TOP_MARGIN    = 0.118  # of pitch height — clear of the far penalty area
ROW_BOTTOM_MARGIN = 0.125  # of pitch height — the keeper, clear of his own box
ROW_SIDE_INSET    = 0.03   # of the row's width, so nobody stands on a touchline
DISC_MAX_D        = 120    # the reference disc, scaled to this canvas
DISC_RING         = 0.065  # ring stroke, as a fraction of the diameter
DISC_NUMBER_H     = 0.60   # number font, as a fraction of the diameter
LABEL_GAP         = 12     # disc bottom → plate top, per the reference
LABEL_H           = 42
LABEL_W           = 168    # the reference plate is fixed-width, every player
LABEL_PAD_X       = 9
LABEL_GUTTER      = 12     # kept clear between one player's plate and the next
DISC_FILL         = 0.72   # of its column, so discs in a row never touch
PALE_ACCENT_LUM   = 195    # above this a colour cannot carry a white disc
DARK_INK_LUM      = 150    # above this a plate needs dark text on it, not white

SHEET_DISC_MAX_D = 72      # the no-positions fallback's smaller disc
SHEET_FONT_MAX   = 40
SHEET_NAME_GAP   = 28


# ── Small helpers ────────────────────────────────────────────────────────────

def _luminance(color) -> float:
    return 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]


def _ink_on(color):
    """Text colour that stays readable on a plate of `color`."""
    return ((20, 24, 30, 255) if _luminance(color) > DARK_INK_LUM
            else (245, 248, 252, 255))


def _team_palette(team_name: str, crest):
    """Curated colours where we have them, sampled from the crest otherwise —
    the same resolution every other card style already uses."""
    curated = TEAM_COLORS.get(team_name)
    if curated:
        return list(curated)
    if crest is None:
        return list(FALLBACK_COLORS)
    try:
        return list(_dominant_colors(crest))
    except Exception:
        return list(FALLBACK_COLORS)


def _accent_color(palette):
    """
    The one colour the whole card is keyed in: the ring, the number and the
    name plates. A white or cream first kit would vanish against the white
    disc, so a pale primary falls through to the second colour, and a pale
    pair to a darkened primary — Fulham stays Fulham-ish rather than grey.
    """
    for color in palette:
        if _luminance(color) <= PALE_ACCENT_LUM:
            return tuple(int(v) for v in color[:3]) + (255,)
    r, g, b = (int(v * 0.45) for v in palette[0][:3])
    return (r, g, b, 255)


def _load_image_url(url: str, what: str):
    """Any remote artwork as a PIL image, or None."""
    if not url:
        return None
    try:
        import io

        import requests
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert('RGBA')
    except Exception as e:
        print(f"[lineup] Could not load {what}: {e}")
        return None


def _load_crest_image(name: str):
    """Team crest as a PIL image, or None — used for colours and the header."""
    return _load_image_url(get_crest_url(name, alert=False), f'crest for {name}')


def _paste_centred(img, crest, box):
    """Fit a crest inside a box without stretching it."""
    if crest is None:
        return
    crest = crest.copy()
    bw, bh = box[2] - box[0], box[3] - box[1]
    crest.thumbnail((bw, bh), Image.LANCZOS)
    img.paste(crest, (box[0] + (bw - crest.width) // 2,
                      box[1] + (bh - crest.height) // 2), crest)


def _draw_left_text(draw, text, box, max_size, min_size, color):
    """Left-aligned, vertically centred — the bench column's only alignment."""
    if not text:
        return
    size = _fit_font_to_box(draw, text, box, FONT_BOLD, max_size, min_size)
    draw.text((box[0], (box[1] + box[3]) / 2), text,
              font=_font(FONT_BOLD, size), fill=color, anchor='lm')


def _side_veil(size):
    """
    A left-to-right darkening gradient, drawn once as a mask.

    The bench sits over whatever part of the stadium photo happens to be
    behind it; a flat panel would cut the page in half, so the veil fades out
    before it reaches the pitch.
    """
    gradient = Image.new('L', (VEIL_WIDTH, 1))
    gradient.putdata([int(255 * (1 - x / VEIL_WIDTH)) for x in range(VEIL_WIDTH)])
    mask = Image.new('L', size, 0)
    mask.paste(gradient.resize((VEIL_WIDTH, size[1])), (0, 0))
    veil = Image.new('RGBA', size, COLOR_SIDE_VEIL[:3] + (0,))
    veil.putalpha(mask.point(lambda v: int(v * COLOR_SIDE_VEIL[3] / 255)))
    return veil


def _surname(name) -> str:
    """'Gerónimo Rulli' → 'RULLI'. The bench column is too narrow for more."""
    parts = str(name or '').strip().split()
    return parts[-1].upper() if parts else ''


def _side_key(side: str) -> str:
    """'home'/'away' → the scraper's team key. Team A is the home side, the
    assumption the scraper URL and every score field already rely on."""
    return 'team_A' if str(side).lower() == 'home' else 'team_B'


def has_lineups(scraper_data: dict, side: str | None = None) -> bool:
    """
    Has the feed published a starting XI yet?

    With no side, both teams must have one: a carousel showing one lineup and
    one empty pitch is worse than waiting a poll for the other to appear.
    """
    formation = scraper_data.get('matchFormation')
    if not isinstance(formation, dict):
        return False
    keys = [_side_key(side)] if side else ['team_A', 'team_B']
    return all(isinstance(formation.get(k), dict)
               and bool(formation[k].get('lineups')) for k in keys)


def has_positions(scraper_data: dict, side: str | None = None) -> bool:
    """
    Are the published XIs placed on the grid, or only listed?

    The feed announces the names first and fills `position_x` / `position_y` in
    later — sometimes not at all for smaller competitions, which is what its
    own `formationFlag` reports. Positions are what the pitch needs; without
    them the page falls back to a team sheet, so this is the signal for *how
    good a card we can draw right now*, not for whether we can draw one.
    """
    if not has_lineups(scraper_data, side):
        return False
    formation = scraper_data['matchFormation']
    keys = [_side_key(side)] if side else ['team_A', 'team_B']
    return all(any(p.get('position_x') for p in formation[k]['lineups'])
               for k in keys)


# ── The grid the feed publishes → rows and columns ───────────────────────────

def _row_rank(band: str) -> int:
    """Sort key for a band, with unknown bands landing ahead of the strikers."""
    band = (band or '').upper()
    return ROW_ORDER.index(band) if band in ROW_ORDER else len(ROW_ORDER)


def build_rows(lineups: list) -> list[list[dict]]:
    """
    The XI grouped into pitch rows, back to front, each row ordered left to
    right. Players the feed left unplaced are appended as their own row rather
    than silently dropped.
    """
    bands: dict[str, list[dict]] = {}
    for player in lineups or []:
        band = str(player.get('position_x') or '?').upper()
        bands.setdefault(band, []).append(player)

    rows = []
    for band in sorted(bands, key=_row_rank):
        players = sorted(bands[band],
                         key=lambda p: SLOT_ORDER.get(
                             str(p.get('position_y') or '').upper(), 2))
        rows.append(players)
    return rows


def formation_label(rows: list[list[dict]]) -> str:
    """'4-2-3-1' — the outfield rows, back to front. The keeper is implied."""
    outfield = [len(row) for row in rows[1:]] if rows else []
    return '-'.join(str(n) for n in outfield)


def _column_x(row: list[dict], left: float, width: float) -> list[float]:
    """
    Where each player in a row sits across the pitch.

    Nearly an even spread, pulled slightly toward the side the feed named —
    matching the reference's own row positions.
    """
    n = len(row)
    xs = []
    for i, player in enumerate(row):
        even = (i + 0.5) / n
        slot = str(player.get('position_y') or '').upper()
        anchor = SLOT_ANCHOR.get(slot, even)
        xs.append(left + width * (even * (1 - ANCHOR_PULL) + anchor * ANCHOR_PULL))
    return xs


# ── The numbered disc ────────────────────────────────────────────────────────

def disc_glyph(diameter: int, accent, number: str):
    """
    A player as the reference card draws one: a white disc, a ring in the
    team's colour, the squad number in the same colour inside it.

    Drawn at 4x and downsampled — Pillow's ellipse edges alias badly at the
    size a lineup needs eleven of.
    """
    scale = 4
    size = diameter * scale
    glyph = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glyph)

    ring = max(2, int(diameter * DISC_RING)) * scale
    draw.ellipse((0, 0, size - 1, size - 1), fill=COLOR_DISC)
    draw.ellipse((ring // 2, ring // 2, size - 1 - ring // 2, size - 1 - ring // 2),
                 outline=accent, width=ring)

    if number:
        font = _font(FONT_BOLD, int(size * DISC_NUMBER_H))
        # Bebas hangs its digits low in the em box; anchor on the glyph bounds
        # so the number sits optically centred in the disc.
        left, top, right, bottom = draw.textbbox((0, 0), str(number), font=font)
        draw.text((size / 2 - (left + right) / 2, size / 2 - (top + bottom) / 2),
                  str(number), font=font, fill=accent)

    return glyph.resize((diameter, diameter), Image.LANCZOS)


# ── The pitch ────────────────────────────────────────────────────────────────

def pitch_edges(y: float) -> tuple[float, float]:
    """The touchlines at depth y. Linear, which is what makes it a trapezoid."""
    t = (y - PITCH_TOP) / (PITCH_BOTTOM - PITCH_TOP)
    half = PITCH_TOP_HALF_W + (PITCH_BOTTOM_HALF_W - PITCH_TOP_HALF_W) * t
    return PITCH_CX - half, PITCH_CX + half


def _span(y: float, fraction: float) -> tuple[float, float]:
    """A centred span of the pitch's width at depth y."""
    left, right = pitch_edges(y)
    half = (right - left) * fraction / 2
    return PITCH_CX - half, PITCH_CX + half


def _box_polygon(y_near: float, y_far: float, fraction: float) -> list:
    """A goal area between two depths, its sides following the perspective."""
    n_l, n_r = _span(y_near, fraction)
    f_l, f_r = _span(y_far, fraction)
    return [(n_l, y_near), (n_r, y_near), (f_r, y_far), (f_l, y_far)]


def draw_pitch(img):
    """
    A full pitch in perspective: the team's own goal at the bottom, the
    opposition's at the top, halfway line and centre circle between them.

    Markings only, no fill — in the reference the stadium shows straight
    through the pitch.
    """
    height = PITCH_BOTTOM - PITCH_TOP
    t_l, t_r = pitch_edges(PITCH_TOP)
    b_l, b_r = pitch_edges(PITCH_BOTTOM)

    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    outline = [(t_l, PITCH_TOP), (t_r, PITCH_TOP),
               (b_r, PITCH_BOTTOM), (b_l, PITCH_BOTTOM)]
    draw.line(outline + [outline[0]], fill=COLOR_LINES, width=LINE_WIDTH)

    # Halfway line and centre circle.
    y_half = PITCH_TOP + height * HALFWAY_DEPTH
    h_l, h_r = pitch_edges(y_half)
    draw.line((h_l, y_half, h_r, y_half), fill=COLOR_LINES, width=LINE_WIDTH)
    c_l, c_r = _span(y_half, CENTRE_CIRCLE_W)
    draw.ellipse((c_l, y_half - height * CENTRE_CIRCLE_H / 2,
                  c_r, y_half + height * CENTRE_CIRCLE_H / 2),
                 outline=COLOR_LINES, width=LINE_WIDTH)

    # Penalty and six-yard areas at both ends, then the goals themselves.
    for y_line, depth_p, depth_s, sign in (
            (PITCH_TOP, PENALTY_DEPTH_FAR, SIX_YARD_FAR, 1),
            (PITCH_BOTTOM, PENALTY_DEPTH_NEAR, SIX_YARD_NEAR, -1)):
        draw.polygon(_box_polygon(y_line, y_line + sign * height * depth_p,
                                  PENALTY_W), outline=COLOR_LINES, width=LINE_WIDTH)
        draw.polygon(_box_polygon(y_line, y_line + sign * height * depth_s,
                                  SIX_YARD_W), outline=COLOR_LINES, width=LINE_WIDTH)
        draw.polygon(_box_polygon(y_line, y_line - sign * height * GOAL_DEPTH,
                                  GOAL_W), outline=COLOR_LINES, width=LINE_WIDTH)

    img.alpha_composite(layer)


# ── Players ──────────────────────────────────────────────────────────────────

def _name_plate(draw, text: str, cx: float, top: float, max_width: float,
                color, ink):
    """
    The player's name on a coloured plate.

    The reference plate is a fixed-width sharp rectangle, identical for every
    player — only the text inside it shrinks to fit.
    """
    if not text:
        return
    width = min(LABEL_W, max_width)
    size = _fit_font_to_box(draw, text,
                            (0, 0, int(width - 2 * LABEL_PAD_X), LABEL_H),
                            FONT_BOLD, PLAYER_FONT_MAX, PLAYER_FONT_MIN)
    font = _font(FONT_BOLD, size)
    draw.rectangle((cx - width / 2, top, cx + width / 2, top + LABEL_H),
                   fill=color)
    draw.text((cx, top + LABEL_H / 2), text, font=font, fill=ink, anchor='mm')


def _player_name(draw, player: dict, max_width: float) -> str:
    """
    The name as it will be drawn: full where it fits, surname where it doesn't.

    Shrinking the font is tried first — Bebas Neue is condensed enough that
    most full names fit the plate — because 'MARTINEZ' twice in one XI tells a
    reader less than 'E. MARTINEZ' and 'L. MARTINEZ' do.
    """
    full = str(player.get('person') or '').strip().upper()
    if not full:
        return ''
    # The reference keeps one text size across the XI; a full name only stays
    # if it fits at close to full size, otherwise the surname does.
    font = _font(FONT_BOLD, PLAYER_FONT_MAX - 4)
    if draw.textlength(full, font=font) <= max_width - 2 * LABEL_PAD_X:
        return full
    return _surname(full)


def draw_players(img, draw, rows, accent):
    """Place every disc and name plate on the pitch."""
    if not rows:
        return
    height = PITCH_BOTTOM - PITCH_TOP
    top = PITCH_TOP + height * ROW_TOP_MARGIN
    bottom = PITCH_BOTTOM - height * ROW_BOTTOM_MARGIN
    step = (bottom - top) / (len(rows) - 1) if len(rows) > 1 else 0

    plate_ink = _ink_on(accent)

    # The busiest row at its own depth decides the disc size for the whole
    # page: eleven discs at different scales would read as a mistake.
    def row_column_w(i, row):
        left, right = pitch_edges(bottom - step * i)
        return (right - left) * (1 - 2 * ROW_SIDE_INSET) / len(row)

    narrowest = min(row_column_w(i, row) for i, row in enumerate(rows))
    disc_d = int(min(DISC_MAX_D, narrowest * DISC_FILL,
                     (step - LABEL_H - LABEL_GAP) if step else DISC_MAX_D))

    for i, row in enumerate(rows):
        # Row 0 is the keeper, drawn nearest the near goal at the bottom.
        cy = bottom - step * i
        left, right = pitch_edges(cy)
        inset = (right - left) * ROW_SIDE_INSET
        left, right = left + inset, right - inset
        column_w = (right - left) / len(row)

        for cx, player in zip(_column_x(row, left, right - left), row):
            disc = disc_glyph(disc_d, accent,
                              str(player.get('shirtnumber') or '').strip())
            img.alpha_composite(disc, (int(cx - disc.width / 2),
                                       int(cy - disc.height / 2)))
            plate_w = column_w - LABEL_GUTTER
            _name_plate(draw, _player_name(draw, player, min(plate_w, LABEL_W)),
                        cx, cy + disc.height / 2 + LABEL_GAP,
                        plate_w, accent, plate_ink)


def draw_team_sheet(img, draw, players: list, box, accent):
    """
    The XI as a list, for a feed that published names without positions.

    One row per player: their disc, then their name. The rest of the page —
    header, bench, coach, footer — is unchanged, so a competition whose feed
    never fills the grid still gets a lineup post rather than nothing.
    """
    x0, y0, x1, y1 = box
    rows = len(players) or 1
    line_h = (y1 - y0) / rows
    disc_d = int(min(SHEET_DISC_MAX_D, line_h * 0.82))

    # The block is centred as a whole, so discs and names share one left edge
    # however long the longest name turns out to be.
    font = _font(FONT_BOLD, min(SHEET_FONT_MAX, int(line_h * 0.62)))
    names = [str(p.get('person') or '').strip().upper() for p in players]
    widest = max([draw.textlength(n, font=font) for n in names] or [0])
    left = (x0 + x1) / 2 - (disc_d + SHEET_NAME_GAP + widest) / 2

    for i, player in enumerate(players):
        cy = y0 + line_h * (i + 0.5)
        disc = disc_glyph(disc_d, accent,
                          str(player.get('shirtnumber') or '').strip())
        img.alpha_composite(disc, (int(left), int(cy - disc.height / 2)))
        draw.text((left + disc_d + SHEET_NAME_GAP, cy), names[i],
                  font=font, fill=COLOR_NAME, anchor='lm')


# ── The left column: bench, then the referee ─────────────────────────────────

def _column_plate(draw, text: str, y: float, height: int, accent, ink):
    """A left-aligned plate in the column — the XI's plate style, off-pitch."""
    size = _fit_font_to_box(draw, text,
                            (0, 0, BENCH_PLATE_W - 2 * LABEL_PAD_X, height),
                            FONT_BOLD, PLAYER_FONT_MAX, PLAYER_FONT_MIN)
    draw.rectangle((COL_L, y, COL_L + BENCH_PLATE_W, y + height), fill=accent)
    draw.text((COL_L + BENCH_PLATE_W / 2, y + height / 2), text,
              font=_font(FONT_BOLD, size), fill=ink, anchor='mm')


def draw_bench(draw, subs: list, accent, ink) -> float:
    """
    The bench down the left — every substitute, surnames only, each on the
    same colour plate the XI's names wear. The reference's 50px rhythm holds
    while it can; a deeper bench compresses the rhythm rather than cutting
    anyone, so the whole squad is always on the card.

    Returns the y it finished at, so the referee sits under a seven-man bench
    as closely as under a twelve-man one.
    """
    _draw_left_text(draw, 'BENCH', BENCH_LABEL_BOX,
                    BENCH_HEAD_MAX, BENCH_FONT_MIN, COLOR_TITLE)
    names = [n for n in (_surname(p.get('person')) for p in subs or []) if n]
    if not names:
        _draw_left_text(draw, 'NOT PUBLISHED',
                        (COL_L, BENCH_TOP, COL_R, BENCH_TOP + BENCH_LINE_H),
                        BENCH_FONT_MAX, BENCH_FONT_MIN, COLOR_BENCH)
        return BENCH_TOP + BENCH_LINE_H

    line_h = min(BENCH_LINE_H, (BENCH_NAMES_MAX_Y - BENCH_TOP) / len(names))
    plate_h = min(LABEL_H, int(line_h) - 8)
    for i, name in enumerate(names):
        _column_plate(draw, name, BENCH_TOP + line_h * i, plate_h, accent, ink)
    return BENCH_TOP + line_h * (len(names) - 1) + plate_h


def draw_referee(draw, referee: str | None, bench_end: float, accent, ink):
    """
    'REFEREE' and the official's name under the bench, closing the left
    column the way the reference's coach block did — the name on the same
    plate style as everyone else's. The referee rides in `matchFormation`
    alongside the lineups, and is routinely empty for smaller competitions,
    in which case the block is simply left off.
    """
    name = str(referee or '').strip()
    if not name:
        return
    y = bench_end + REF_GAP
    _draw_left_text(draw, 'REFEREE', (COL_L, y, COL_R, y + REF_LABEL_H),
                    BENCH_HEAD_MAX - 8, BENCH_FONT_MIN, COLOR_TITLE)
    _column_plate(draw, name.upper(), y + REF_LABEL_H + REF_NAME_GAP,
                  LABEL_H, accent, ink)


# ── The footer strip ─────────────────────────────────────────────────────────

def draw_footer(img, draw):
    """
    'BROUGHT TO YOU BY' and the brand mark, centred on the page as one group —
    the reference card's whole bottom strip.
    """
    text = 'BROUGHT TO YOU BY'
    font = _font(FONT_BOLD, FOOTER_FONT_MAX)
    text_w = draw.textlength(text, font=font)

    logo = _load_image_url(get_brand_logo_url(), 'brand logo')
    logo_w = 0
    if logo is not None:
        logo = logo.copy()
        logo.thumbnail((FOOTER_LOGO_H * 4, FOOTER_LOGO_H), Image.LANCZOS)
        logo_w = logo.width + FOOTER_GAP

    left = img.width / 2 - (text_w + logo_w) / 2
    draw.text((left, FOOTER_CY), text, font=font, fill=COLOR_TITLE, anchor='lm')
    if logo is not None:
        img.alpha_composite(logo, (int(left + text_w + FOOTER_GAP),
                                   int(FOOTER_CY - logo.height / 2)))


# ── Main public function ─────────────────────────────────────────────────────

def _card_background(competition, match_id):
    """The shared match template at the card's own size, pushed back to night:
    blur, then a heavy flat veil.

    The templates are already 1086x1448, so this is normally a straight open;
    anything a different size is centre-cropped, or scaled up first when it is
    too small to crop from."""
    img = load_template(competition, match_id)
    if img.width < CANVAS_W or img.height < CANVAS_H:
        scale = max(CANVAS_W / img.width, CANVAS_H / img.height)
        img = img.resize((max(CANVAS_W, int(img.width * scale)),
                          max(CANVAS_H, int(img.height * scale))), Image.LANCZOS)
    x0 = (img.width - CANVAS_W) // 2
    y0 = (img.height - CANVAS_H) // 2
    img = img.crop((x0, y0, x0 + CANVAS_W, y0 + CANVAS_H))
    img = img.filter(ImageFilter.GaussianBlur(BG_BLUR))
    img = Image.alpha_composite(img, Image.new('RGBA', img.size,
                                               (6, 9, 15, BG_DARKEN)))
    img.alpha_composite(_side_veil(img.size))
    return img


def generate_lineup_card(scraper_data: dict, side: str = 'home',
                         match_id_override: str = '',
                         home_name: str | None = None,
                         away_name: str | None = None,
                         competition: str | None = None,
                         coach: str | None = None) -> str | None:
    """
    Build one team's starting XI page.

    side: 'home' or 'away' — which team this page is about.
    coach: accepted for compatibility with existing callers, no longer drawn —
    the left column closes with the referee, which the feed itself publishes.
    Returns the local path of the saved PNG, or None when that team has no XI
    published yet (the caller retries on its next poll).
    """
    match_sample = scraper_data.get('matchSample', {})
    raw_home = match_sample.get('team_A_name', 'Home')
    raw_away = match_sample.get('team_B_name', 'Away')
    # Display / crest names: the validated matches.json names win over the
    # scraper's spelling.
    home_team = home_name or TEAM_NAME_ALIASES.get(raw_home, raw_home)
    away_team = away_name or TEAM_NAME_ALIASES.get(raw_away, raw_away)
    match_id = str(match_sample.get('match_id') or match_id_override or 'unknown')

    is_home = str(side).lower() == 'home'
    team = home_team if is_home else away_team

    formation = scraper_data.get('matchFormation') or {}
    team_block = formation.get(_side_key(side)) or {}
    players = team_block.get('lineups') or []
    if not players:
        print(f"[lineup] {match_id}: no {side} XI published yet.")
        return None
    # Positions are what makes a pitch; without them the page lists the XI.
    placed = any(p.get('position_x') for p in players)
    rows = build_rows(players) if placed else []

    competition = competition or match_sample.get('competition_name')
    img = _card_background(competition, match_id)

    crest = _load_crest_image(team)
    accent = _accent_color(_team_palette(team, crest))

    # Pitch and discs are composited straight onto the background; text and
    # name plates go on this layer, shadowed and composited in one pass.
    if placed:
        draw_pitch(img)
    ink = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ink)

    # ── Header: both crests at full strength either side of the VS ───────────
    home_crest = crest if is_home else _load_crest_image(home_team)
    away_crest = _load_crest_image(away_team) if is_home else crest
    _paste_centred(img, home_crest, HOME_CREST_BOX)
    _paste_centred(img, away_crest, AWAY_CREST_BOX)
    _draw_centered_text(draw, 'VS', VS_BOX, FONT_BOLD,
                        VS_FONT_MAX, TITLE_FONT_MIN, COLOR_TITLE)
    _draw_centered_text(draw, 'STARTING XI', TITLE_BOX, FONT_BOLD,
                        TITLE_FONT_MAX, TITLE_FONT_MIN, COLOR_TITLE)

    # ── The XI, and the column beside it ─────────────────────────────────────
    if placed:
        draw_players(img, draw, rows, accent)
    else:
        print(f"[lineup] {match_id}: {side} XI has no positions — listing it instead.")
        draw_team_sheet(img, draw, players,
                        (PITCH_CX - PITCH_BOTTOM_HALF_W, PITCH_TOP,
                         PITCH_CX + PITCH_BOTTOM_HALF_W, PITCH_BOTTOM),
                        accent)

    plate_ink = _ink_on(accent)
    bench_end = draw_bench(draw, team_block.get('sub'), accent, plate_ink)
    draw_referee(draw, formation.get('referee'), bench_end, accent, plate_ink)
    draw_footer(img, draw)

    img = Image.alpha_composite(img, _shadow_of(ink))
    img = Image.alpha_composite(img, ink)

    os.makedirs('output', exist_ok=True)
    out = f"output/lineup_{match_id}_{'home' if is_home else 'away'}.png"
    img.convert('RGB').save(out, quality=95)
    print(f"[lineup] Saved: {out}")
    return out


if __name__ == '__main__':
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else 'data/54460142-Arsenal-vs-Man-City.json'
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    for which in ('home', 'away'):
        generate_lineup_card(data, side=which)
