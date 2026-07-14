# Scorecard overlay for match photos, replicating the layout of
# scorecard_graphic.jpeg: a closed chamfered-polygon glass panel at the bottom
# of a 3:4 photo, tournament logo centered ON the panel's top border line,
# FULL/HALF TIME label, big score with divider, Cloudinary team crests,
# team-colored partition lines, and scorecard.py-style scorer lines
# ([symbol] [minute] [name] for home, mirrored for away).
#
# The panel height is dynamic: it grows with the number of scorer lines
# (minimum height = one-line panel, also used when a side has no scorers).

import io
import json
import os
import sys

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import (
    BRAND_LOGO,
    CLOUD_NAME,
    CLOUDINARY_TOURNAMENT_LOGO,
    LOCAL_SYMBOLS,
    TEAM_NAME_ALIASES,
    get_crest_url,
)

FONT_PATH = 'assets/fonts/BebasNeue-Regular.ttf'

COLOR_WHITE = (245, 247, 250, 255)
COLOR_GOLD = (212, 175, 90, 255)
COLOR_SCORER = (230, 233, 240, 255)
COLOR_MINUTE = (255, 200, 60, 255)
COLOR_LABEL = (212, 175, 90, 255)   # FULL TIME / HALF TIME / PENALTIES
PANEL_FILL = (8, 10, 14, 105)
PANEL_BORDER = (212, 175, 90, 130)

# Curated primary/secondary colors per team for the partition line under the
# team name. Teams not listed here get colors auto-derived from their crest.
TEAM_COLORS = {
    'Argentina':   [(117, 170, 219), (255, 255, 255)],
    'Egypt':       [(206, 17, 38), (0, 0, 0)],
    'Portugal':    [(0, 102, 51), (218, 41, 28)],
    'Spain':       [(198, 11, 30), (255, 196, 0)],
    'Switzerland': [(213, 43, 30), (255, 255, 255)],
    'France':      [(0, 85, 164), (239, 65, 53)],
    'Morocco':     [(193, 21, 35), (0, 98, 51)],
    'Norway':      [(186, 12, 47), (0, 32, 91)],
    'England':     [(206, 17, 38), (255, 255, 255)],
    'Belgium':     [(0, 0, 0), (255, 205, 0)],
}
FALLBACK_COLORS = [(212, 175, 90), (245, 247, 250)]

# Event types shown as scorer lines — same set scorecard.py displays.
DISPLAY_TYPES = {'goal', 'penalty_goal', 'own_goal', 'red_card', 'penalty_missed'}

MAX_VISIBLE_LINES = 6          # beyond this, collapse into "& N more"
SCORER_FONT_MAX_RATIO = 0.021  # of image height
SCORER_FONT_MIN_RATIO = 0.013
SYMBOL_TEXT_GAP_RATIO = 0.35   # of scorer font size
MINUTE_NAME_GAP_RATIO = 0.45


# ── Basics ────────────────────────────────────────────────────────────────────

def _font(size):
    try:
        return ImageFont.truetype(FONT_PATH, int(size))
    except Exception:
        return ImageFont.load_default()


def _text_size(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _crop_to_aspect(img, target_ratio):
    w, h = img.size
    if w / h > target_ratio:
        new_w = round(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = round(w / target_ratio)
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


# ── Remote assets ─────────────────────────────────────────────────────────────

def _fetch_cloudinary_image(public_id):
    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{public_id}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert('RGBA')


def _load_wc_logo(height):
    """Tournament logo, cropped to drop the black 'FIFA' wordmark row (illegible
    on the dark panel) and trimmed tight to content."""
    try:
        logo = _fetch_cloudinary_image(CLOUDINARY_TOURNAMENT_LOGO['World Cup'])
    except Exception as e:
        print(f"[overlay] Could not fetch World Cup logo: {e}")
        return None
    w, h = logo.size
    logo = logo.crop((0, 0, w, int(h * 0.81)))
    bbox = logo.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    ratio = height / logo.height
    return logo.resize((max(1, int(logo.width * ratio)), int(height)), Image.LANCZOS)


def _load_brand_logo(height):
    try:
        logo = _fetch_cloudinary_image(BRAND_LOGO['130 Yards'])
    except Exception as e:
        print(f"[overlay] Could not fetch 130 Yards logo: {e}")
        return None
    bbox = logo.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    ratio = height / logo.height
    return logo.resize((max(1, int(logo.width * ratio)), int(height)), Image.LANCZOS)


def _fetch_crest(team_name, height):
    url = get_crest_url(team_name)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        crest = Image.open(io.BytesIO(r.content)).convert('RGBA')
    except Exception as e:
        print(f"[overlay] Could not fetch crest for {team_name}: {e}")
        return None
    bbox = crest.getbbox()
    if bbox:
        crest = crest.crop(bbox)
    ratio = height / crest.height
    return crest.resize((max(1, int(crest.width * ratio)), int(height)), Image.LANCZOS)


def _dominant_colors(crest, k=2):
    """Two most prominent colors in a crest image, for teams without curated
    colors. Prefers saturated brand colors over white/black fills, with a
    minimum mutual distance so the two segments read as distinct."""
    arr = np.array(crest.convert('RGBA').resize((64, 64), Image.LANCZOS))
    px = arr[arr[..., 3] > 128][:, :3].astype(np.int32)
    if len(px) == 0:
        return FALLBACK_COLORS
    q = (px // 32) * 32
    colors, counts = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
    vivid = ~(((colors > 190).all(axis=1)) | ((colors < 50).all(axis=1)))
    if vivid.sum() >= k:
        colors, counts = colors[vivid], counts[vivid]
    picked = []
    for idx in np.argsort(-counts):
        c = tuple(int(v) + 16 for v in colors[idx])
        if all(sum(abs(a - b) for a, b in zip(c, p)) > 90 for p in picked):
            picked.append(c)
        if len(picked) == k:
            break
    while len(picked) < k:
        picked.append(FALLBACK_COLORS[len(picked) % len(FALLBACK_COLORS)])
    return picked


def _drop_shadow(glyph, blur=6, alpha=160):
    alpha_ch = glyph.split()[3]
    shadow = Image.new('RGBA', glyph.size, (0, 0, 0, 0))
    shadow.putalpha(alpha_ch.point(lambda a: alpha if a > 0 else 0))
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def _glow(diameter, color, blur_ratio=0.5, alpha=120):
    size = int(diameter * 2.2)
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (size // 2 - diameter // 2, size // 2 - diameter // 2,
         size // 2 + diameter // 2, size // 2 + diameter // 2),
        fill=(*color[:3], alpha),
    )
    return glow.filter(ImageFilter.GaussianBlur(radius=diameter * blur_ratio))


# ── Symbols ───────────────────────────────────────────────────────────────────

def _card_symbol(height, color):
    """Draw a booking-card glyph natively — the card assets on disk are JPEGs
    with no alpha channel and would show as hard rectangles on the panel."""
    ch = int(height)
    cw = max(2, int(ch * 0.72))
    card = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    r = max(2, ch // 8)
    d.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=r, fill=(*color, 255),
                        outline=(255, 255, 255, 200), width=1)
    return card


def _event_symbol(event_type, height):
    if event_type == 'red_card':
        return _card_symbol(height, (206, 30, 30))
    if event_type == 'yellow_card':
        return _card_symbol(height, (240, 200, 20))
    mapping = {'penalty_goal': 'penalty_goal', 'own_goal': 'own_goal', 'penalty_missed': 'penalty_missed'}
    path = LOCAL_SYMBOLS.get(mapping.get(event_type, 'normal_goal'), '')
    if not path:
        return None
    try:
        sym = Image.open(path).convert('RGBA')
        ratio = height / sym.height
        return sym.resize((max(1, int(sym.width * ratio)), int(height)), Image.LANCZOS)
    except Exception:
        return None


# ── Event extraction (scorecard.py formatting rules) ──────────────────────────

def _format_player_name(full_name):
    parts = (full_name or '?').strip().split()
    if len(parts) <= 1:
        return (full_name or '?').upper()
    return f"{parts[0][0]}. {parts[-1]}".upper()


def _extract_scorer_lines(events, team_name):
    """Displayable events for team_name, chronological, scorecard.py-style."""
    lines = []
    if not isinstance(events, list):
        return lines
    for ev in events:
        if ev.get('type') not in DISPLAY_TYPES:
            continue
        if ev.get('team') != team_name:
            continue
        lines.append({
            'type': ev['type'],
            'minute': ev.get('minute', '?'),
            'name': _format_player_name(ev.get('player', '?')),
        })
    return lines


# ── Panel geometry ────────────────────────────────────────────────────────────

def _panel_polygon(x1, y1, x2, y2, chamfer):
    """Closed polygon with chamfered top-left and bottom-right corners,
    matching the reference graphic."""
    return [
        (x1 + chamfer, y1), (x2, y1), (x2, y2 - chamfer),
        (x2 - chamfer, y2), (x1, y2), (x1, y1 + chamfer),
    ]


def _draw_glass_panel(img, box, chamfer):
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1

    region = img.crop(box).convert('RGB')
    region = region.filter(ImageFilter.GaussianBlur(radius=14))
    region = region.convert('RGBA')
    region.alpha_composite(Image.new('RGBA', (w, h), PANEL_FILL))

    local_pts = _panel_polygon(0, 0, w - 1, h - 1, chamfer)
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).polygon(local_pts, fill=255)

    panel = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    panel.paste(region, (0, 0), mask)

    border = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(border).polygon(local_pts, outline=PANEL_BORDER, width=3)
    panel.alpha_composite(border)

    img.alpha_composite(panel, (x1, y1))


def _apply_bottom_fade(img, panel_top_y, blend_h, max_alpha=185):
    """Black gradient that blends the photo into the graphic: it ramps from
    fully transparent down to max_alpha across `blend_h` pixels ending exactly
    at the panel's top edge, then holds that darkness behind the panel to the
    bottom of the image. Nothing above the ramp is touched."""
    w, h = img.size
    y0 = max(0, panel_top_y - blend_h)
    total_h = h - y0
    if total_h <= 0:
        return
    ramp_len = panel_top_y - y0
    t = np.ones(total_h, dtype=np.float32)
    if ramp_len > 0:
        t[:ramp_len] = np.linspace(0.0, 1.0, ramp_len, dtype=np.float32) ** 1.2
    alpha_col = (t * max_alpha).astype(np.uint8)
    gradient = np.zeros((total_h, w, 4), dtype=np.uint8)
    gradient[..., 3] = alpha_col.reshape(-1, 1)
    img.alpha_composite(Image.fromarray(gradient, mode='RGBA'), (0, y0))


# ── Scorer lines rendering (scorecard.py layout, panel-scaled) ────────────────

def _fit_scorer_font(draw, home_lines, away_lines, col_w, h):
    """Largest font size (within ratio bounds) where every line fits its column."""
    max_size = int(h * SCORER_FONT_MAX_RATIO)
    min_size = max(10, int(h * SCORER_FONT_MIN_RATIO))
    for size in range(max_size, min_size - 1, -1):
        font = _font(size)
        _, lh = _text_size(draw, "Ag", font)
        ok = True
        for entry in home_lines + away_lines:
            mw, _ = _text_size(draw, entry['minute'], font)
            nw, _ = _text_size(draw, entry['name'], font)
            total = lh + int(size * SYMBOL_TEXT_GAP_RATIO) + mw + int(size * MINUTE_NAME_GAP_RATIO) + nw
            if total > col_w:
                ok = False
                break
        if ok:
            return size
    return min_size


def _truncate_name(draw, name, font, max_w):
    if _text_size(draw, name, font)[0] <= max_w:
        return name
    while len(name) > 1:
        name = name[:-1]
        if _text_size(draw, name + '…', font)[0] <= max_w:
            return name + '…'
    return name


def _draw_scorer_lines(img, draw, lines, box, align, font_size, step):
    """
    HOME (left):  [symbol] [gap] [minute] [gap] [name]
    AWAY (right): [name] [gap] [minute] [gap] [symbol]
    Minute in gold, name in white — same as scorecard.py.
    """
    x1, y1, x2, _ = box
    font = _font(font_size)
    _, lh = _text_size(draw, "Ag", font)
    sym_gap = int(font_size * SYMBOL_TEXT_GAP_RATIO)
    min_gap = int(font_size * MINUTE_NAME_GAP_RATIO)

    visible = lines[:MAX_VISIBLE_LINES]
    hidden = len(lines) - len(visible)

    for i, entry in enumerate(visible):
        # Anchor text and symbol to the same vertical midline so the symbol
        # sits flush with the glyphs instead of floating above them.
        cy_mid = y1 + i * step + lh // 2
        sym = _event_symbol(entry['type'], lh)
        sw = sym.width if sym else 0
        mw, _ = _text_size(draw, entry['minute'], font)

        max_name_w = (x2 - x1) - sw - sym_gap - mw - min_gap
        name = _truncate_name(draw, entry['name'], font, max_name_w)

        if align == 'left':
            sym_x = x1
            minute_x = x1 + sw + sym_gap
            name_x = minute_x + mw + min_gap
            draw.text((minute_x, cy_mid), entry['minute'], font=font, fill=COLOR_MINUTE, anchor='lm')
            draw.text((name_x, cy_mid), name, font=font, fill=COLOR_SCORER, anchor='lm')
        else:
            sym_x = x2 - sw
            minute_x = sym_x - sym_gap
            draw.text((minute_x, cy_mid), entry['minute'], font=font, fill=COLOR_MINUTE, anchor='rm')
            draw.text((minute_x - mw - min_gap, cy_mid), name, font=font, fill=COLOR_SCORER, anchor='rm')

        if sym:
            img.alpha_composite(sym, (int(sym_x), int(cy_mid - sym.height // 2)))

    if hidden > 0:
        fy = y1 + len(visible) * step
        more = f"& {hidden} MORE"
        mx = x1 if align == 'left' else x2
        draw.text((mx, fy), more, font=font, fill=(160, 170, 190, 255),
                  anchor='la' if align == 'left' else 'ra')

    if not lines:
        mx = x1 if align == 'left' else x2
        draw.text((mx, y1), '-', font=font, fill=COLOR_SCORER,
                  anchor='la' if align == 'left' else 'ra')


# ── Main renderer ─────────────────────────────────────────────────────────────

def add_scorecard_overlay(image_path, output_path, home_team, away_team,
                           home_score, away_score, event_type='FT',
                           penalties=None, home_events=None, away_events=None):
    """
    penalties: optional (home_pen, away_pen) strings shown as
               'PENALTIES: h - a' below the final score.
    """
    home_events = home_events or []
    away_events = away_events or []

    img = Image.open(image_path).convert('RGBA')
    img = _crop_to_aspect(img, 3 / 4)
    w, h = img.size
    draw = ImageDraw.Draw(img)

    # ── Geometry: fixed header block + dynamic scorer block ──────────────
    px1 = int(w * 0.045)
    px2 = w - px1
    pw = px2 - px1
    cx = (px1 + px2) // 2
    py2 = h - int(h * 0.025)

    n_lines = max(1, min(len(home_events), MAX_VISIBLE_LINES + 1),
                  min(len(away_events), MAX_VISIBLE_LINES + 1))

    scorer_col_w = int(pw * 0.42)
    scorer_size = _fit_scorer_font(draw, home_events, away_events, scorer_col_w, h)
    _, scorer_lh = _text_size(draw, "Ag", _font(scorer_size))
    line_step = scorer_lh + int(h * 0.012)

    header_h = int(h * 0.215)                     # logo half → underline → scorer top
    panel_h = header_h + n_lines * line_step + int(h * 0.022)
    py1 = py2 - panel_h
    panel_box = (px1, py1, px2, py2)

    # ── Bottom fade: blends into the panel, ending at its top edge ───────
    _apply_bottom_fade(img, panel_top_y=py1, blend_h=int(h * 0.11))

    # ── Closed chamfered glass panel ─────────────────────────────────────
    _draw_glass_panel(img, panel_box, chamfer=int(h * 0.035))
    draw = ImageDraw.Draw(img)

    # ── Tournament logo centered ON the top border line ──────────────────
    wc_logo = _load_wc_logo(int(h * 0.058))
    if wc_logo:
        img.alpha_composite(_drop_shadow(wc_logo, blur=8, alpha=140),
                            (cx - wc_logo.width // 2, py1 - wc_logo.height // 2))
        img.alpha_composite(wc_logo, (cx - wc_logo.width // 2, py1 - wc_logo.height // 2))
        draw = ImageDraw.Draw(img)

    # ── FULL TIME / HALF TIME label ───────────────────────────────────────
    label = 'FULL TIME' if event_type.upper() == 'FT' else 'HALF TIME'
    label_font = _font(int(h * 0.016))
    label_y = py1 + int(h * 0.043)
    # letter-spaced label, like the reference's tracked-out team names
    spaced = ' '.join(label)
    draw.text((cx, label_y), spaced, font=label_font, fill=COLOR_LABEL, anchor='mm')

    # ── Score row: crests + big score + divider ───────────────────────────
    score_cy = py1 + int(h * 0.104)
    crest_h = int(h * 0.082)
    home_ccx = px1 + int(pw * 0.17)
    away_ccx = px2 - int(pw * 0.17)

    home_crest = _fetch_crest(home_team, crest_h)
    away_crest = _fetch_crest(away_team, crest_h)

    # Curated colors when available, otherwise derived from the crest itself
    home_colors = TEAM_COLORS.get(home_team) or (_dominant_colors(home_crest) if home_crest else FALLBACK_COLORS)
    away_colors = TEAM_COLORS.get(away_team) or (_dominant_colors(away_crest) if away_crest else FALLBACK_COLORS)

    for ccx, colors in ((home_ccx, home_colors), (away_ccx, away_colors)):
        g = _glow(crest_h, colors[0])
        img.alpha_composite(g, (ccx - g.width // 2, score_cy - g.height // 2))

    if home_crest:
        img.alpha_composite(home_crest, (home_ccx - home_crest.width // 2, score_cy - crest_h // 2))
    if away_crest:
        img.alpha_composite(away_crest, (away_ccx - away_crest.width // 2, score_cy - crest_h // 2))
    draw = ImageDraw.Draw(img)

    score_font = _font(int(h * 0.094))
    divider_h = int(h * 0.075)
    draw.line((cx, score_cy - divider_h // 2, cx, score_cy + divider_h // 2),
              fill=COLOR_GOLD, width=2)
    score_gap = int(pw * 0.06)
    draw.text((cx - score_gap, score_cy), str(home_score), font=score_font, fill=COLOR_WHITE, anchor='mm')
    draw.text((cx + score_gap, score_cy), str(away_score), font=score_font, fill=COLOR_WHITE, anchor='mm')

    # ── Team names + team-colored partition line ──────────────────────────
    name_y = py1 + int(h * 0.168)
    name_font = _font(int(h * 0.023))
    for ccx, team in ((home_ccx, home_team), (away_ccx, away_team)):
        draw.text((ccx, name_y), ' '.join(team.upper()), font=name_font, fill=COLOR_WHITE, anchor='mm')

    partition_w = int(pw * 0.24)
    partition_y = py1 + int(h * 0.188)
    for ccx, colors in ((home_ccx, home_colors), (away_ccx, away_colors)):
        seg_w = partition_w / len(colors)
        sx = ccx - partition_w / 2
        for i, col in enumerate(colors):
            draw.line((sx + i * seg_w, partition_y, sx + (i + 1) * seg_w, partition_y),
                      fill=(*col, 255), width=3)

    # ── PENALTIES line (only when the match went to a shootout) ───────────
    if penalties:
        pen_font = _font(int(h * 0.017))
        pen_text = f"PENALTIES: {penalties[0]} - {penalties[1]}"
        draw.text((cx, py1 + int(h * 0.155)), pen_text, font=pen_font, fill=COLOR_LABEL, anchor='mm')

    # ── Scorer lines ──────────────────────────────────────────────────────
    scorers_top = py1 + header_h
    home_box = (px1 + int(pw * 0.05), scorers_top, px1 + int(pw * 0.05) + scorer_col_w, py2)
    away_box = (px2 - int(pw * 0.05) - scorer_col_w, scorers_top, px2 - int(pw * 0.05), py2)
    _draw_scorer_lines(img, draw, home_events, home_box, 'left', scorer_size, line_step)
    _draw_scorer_lines(img, draw, away_events, away_box, 'right', scorer_size, line_step)

    # ── 130 Yards brand logo, small, top-left of the photo ────────────────
    brand_logo = _load_brand_logo(int(h * 0.034))
    if brand_logo:
        bx, by = int(w * 0.035), int(h * 0.025)
        img.alpha_composite(_drop_shadow(brand_logo), (bx, by))
        img.alpha_composite(brand_logo, (bx, by))

    img.convert('RGB').save(output_path, quality=95)
    print(f"Saved: {output_path} ({w}x{h}, panel {panel_h}px, {n_lines} scorer lines)")


# ── Pipeline entry points (scraper_data format, like scorecard.py) ────────────

def generate_overlay_scorecard(scraper_data, photo_path, event_type='FT',
                                match_id_override=''):
    """Photo-based counterpart of scorecard.generate_scorecard: renders the
    overlay onto photo_path and returns the saved output path."""
    sample = scraper_data.get('matchSample', {})
    match_id = str(sample.get('match_id') or match_id_override or 'unknown')
    os.makedirs('output', exist_ok=True)
    output_path = f"output/scorecard_{match_id}_{event_type}_overlay.png"
    _render_from_scraper_data(scraper_data, photo_path, output_path, event_type)
    return output_path


def add_scorecard_overlay_from_json(image_path, output_path, data_path, event_type='FT'):
    _render_from_scraper_data(json.load(open(data_path)), image_path, output_path, event_type)


def _render_from_scraper_data(data, image_path, output_path, event_type='FT'):
    sample = data['matchSample']
    raw_home = sample.get('team_A_name', 'Home')
    raw_away = sample.get('team_B_name', 'Away')
    home_team = TEAM_NAME_ALIASES.get(raw_home, raw_home)
    away_team = TEAM_NAME_ALIASES.get(raw_away, raw_away)
    events = data.get('events', [])

    ps_home = str(sample.get('ps_A') or '').strip()
    ps_away = str(sample.get('ps_B') or '').strip()
    penalties = (ps_home, ps_away) if (ps_home and ps_away and event_type == 'FT') else None

    if event_type == 'HT':
        home_score = str(sample.get('hts_A') or '0')
        away_score = str(sample.get('hts_B') or '0')
    else:
        home_score = str(sample.get('fs_A') or sample.get('hts_A') or '0')
        away_score = str(sample.get('fs_B') or sample.get('hts_B') or '0')
        if penalties:
            # fs_* includes shootout goals; back them out, same as scorecard.py
            try:
                home_score = str(int(home_score) - int(ps_home))
                away_score = str(int(away_score) - int(ps_away))
            except ValueError:
                pass

    # Exclude 120' shootout events from the scorer list, same as scorecard.py.
    # Only penalty kicks are shootout events — open-play/own goals at 120'
    # (e.g. scored in ET injury time) must still be shown.
    filtered = events
    if penalties:
        filtered = [e for e in events
                    if not (e.get('minute') == "120'"
                            and e.get('type') in ('penalty_goal', 'penalty_missed'))]

    add_scorecard_overlay(
        image_path, output_path,
        home_team=home_team, away_team=away_team,
        home_score=home_score, away_score=away_score,
        event_type=event_type, penalties=penalties,
        home_events=_extract_scorer_lines(filtered, raw_home),
        away_events=_extract_scorer_lines(filtered, raw_away),
    )


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'sample_upscaled.png'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'sample_scorebar.png'
    data_path = sys.argv[3] if len(sys.argv) > 3 else 'data/54328023-Argentina-vs-Egypt.json'
    evt = sys.argv[4] if len(sys.argv) > 4 else 'FT'
    add_scorecard_overlay_from_json(src, dst, data_path, event_type=evt)
