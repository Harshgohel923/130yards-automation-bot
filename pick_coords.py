"""
pick_coords.py — Bounding-box picker for scorecard templates.

Click TOP-LEFT then BOTTOM-RIGHT to define each zone. The boxes you pick are
printed as paste-ready constants for the top of scorecard.py.

USAGE
    python pick_coords.py <template.png>        # a local template file
    python pick_coords.py --cloudinary UCL      # a key from CLOUDINARY_TEMPLATES
    python pick_coords.py --cloudinary random-1

GUIDES  (all toggleable — the centre lines are on by default)
    G   vertical + horizontal centre lines
    T   rule-of-thirds grid
    B   safe-area border (5% inset)

PLACEMENT HELPERS
    M   mirror the paired zone (away crest = mirrored home crest, etc.)
    C   centre the last box horizontally on the vertical axis
    S   skip this zone (records (0,0,0,0) — the renderer then omits it)
    R   redo the last box
    Q / ESC   finish and print the summary

While hovering, the readout shows the cursor's exact template coordinates and
its offset from the vertical centre line — so symmetric layouts can be matched
by eye and confirmed by number.

REQUIREMENTS
    pip install opencv-python
"""

import argparse
import os
import sys

import cv2
import numpy as np

DEFAULT_TEMPLATE = "scorecard-template-UCL.png"
LOG_FILE    = "coords_log.txt"
MAX_H       = 900          # display height cap; the picker still records full-res coords
PANEL_W     = 430
WINDOW_NAME = "Box Picker — click TOP-LEFT then BOTTOM-RIGHT of each zone"

# ── Zone definitions ────────────────────────────────────────────────────────
# Order follows the card top-to-bottom so picking reads naturally.

ZONES = [
    ("EVENT_TITLE_BOX",   "FULL TIME / HALF TIME", "the big status headline"),
    ("BRAND_LOGO_BOX",    "130 Yards logo",        "page mark, usually a corner"),
    ("COMP_LOGO_BOX",     "Competition logo",      "PL / UCL badge, friendly logo"),
    ("GROUP_STAGE_BOX",   "League / round label",  "'PREMIER LEAGUE | MATCHDAY 4'"),
    ("HOME_CREST_BOX",    "Home crest",            "left badge"),
    ("AWAY_CREST_BOX",    "Away crest",            "right badge"),
    ("HOME_SCORE_BOX",    "Home score",            "large left digit"),
    ("AWAY_SCORE_BOX",    "Away score",            "large right digit"),
    ("HOME_NAME_BOX",     "Home team name",        "left name text"),
    ("AWAY_NAME_BOX",     "Away team name",        "right name text"),
    ("PENALTY_SCORE_BOX", "Penalty shootout",      "'(4 - 2 pen)'"),
    ("HOME_SCORERS_BOX",  "Home scorers",          "goal lines, left column"),
    ("AWAY_SCORERS_BOX",  "Away scorers",          "goal lines, right column"),
    ("STADIUM_BOX",       "Stadium name",          "bottom centre text"),
]

ZONE_COLORS = [
    (255, 255,  60),   # event title
    (150, 255, 190),   # brand logo
    (120, 255, 255),   # comp logo
    (0,   220, 255),   # league/round
    (255, 160,   0),   # home crest
    (255,  60, 200),   # away crest
    (0,   255, 128),   # home score
    (80,  180, 255),   # away score
    (0,   200, 255),   # home name
    (255, 120, 220),   # away name
    (255,  80,  80),   # penalty
    (200, 100, 255),   # home scorers
    (60,  220, 160),   # away scorers
    (255, 200, 120),   # stadium
]

# Zones that mirror each other across the vertical centre line, so 'M' can
# derive one from the other instead of clicking it by hand.
MIRROR_PAIRS = {
    "AWAY_CREST_BOX":   "HOME_CREST_BOX",
    "HOME_CREST_BOX":   "AWAY_CREST_BOX",
    "AWAY_SCORE_BOX":   "HOME_SCORE_BOX",
    "HOME_SCORE_BOX":   "AWAY_SCORE_BOX",
    "AWAY_NAME_BOX":    "HOME_NAME_BOX",
    "HOME_NAME_BOX":    "AWAY_NAME_BOX",
    "AWAY_SCORERS_BOX": "HOME_SCORERS_BOX",
    "HOME_SCORERS_BOX": "AWAY_SCORERS_BOX",
}

NAMES = [z[0] for z in ZONES]
TOTAL_ZONES = len(ZONES)
EMPTY = (0, 0, 0, 0)

# ── State ────────────────────────────────────────────────────────────────────

boxes    = []       # finished boxes, index-aligned with ZONES
cur_pt1  = None     # first click of the in-progress box
mouse_x  = 0
mouse_y  = 0
scale    = 1.0
img_w    = 0        # full-resolution template size
img_h    = 0
show_center = True
show_thirds = False
show_safe   = False


# ── Image loading ────────────────────────────────────────────────────────────

def load_image(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Template not found: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        # Flatten transparency onto a dark background so alpha templates are visible.
        bgr   = img[:, :, :3].astype(np.float32)
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        bg    = np.full_like(bgr, 28.0)
        img   = (bgr * alpha + bg * (1 - alpha)).astype(np.uint8)
    else:
        img = img[:, :, :3]
    return img


def fetch_from_cloudinary(key):
    """Download a template by its CLOUDINARY_TEMPLATES key and return the local path."""
    import requests
    from config import CLOUD_NAME, CLOUDINARY_TEMPLATES

    public_id = CLOUDINARY_TEMPLATES.get(key)
    if not public_id:
        known = ', '.join(sorted(CLOUDINARY_TEMPLATES))
        raise SystemExit(f"Unknown template key '{key}'. Known keys: {known}")

    os.makedirs('assets/templates', exist_ok=True)
    local_path = os.path.join('assets/templates', f'{key}.png')
    if os.path.exists(local_path):
        return local_path

    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{public_id}"
    print(f"[picker] Downloading template '{key}' from Cloudinary…")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    with open(local_path, 'wb') as f:
        f.write(r.content)
    return local_path


# ── Guides ───────────────────────────────────────────────────────────────────

def draw_guides(canvas, w, h):
    """Centre lines, thirds and safe area, drawn under the boxes."""
    if show_thirds:
        for i in (1, 2):
            x = int(w * i / 3)
            y = int(h * i / 3)
            cv2.line(canvas, (x, 0), (x, h - 1), (70, 70, 90), 1, cv2.LINE_AA)
            cv2.line(canvas, (0, y), (w - 1, y), (70, 70, 90), 1, cv2.LINE_AA)

    if show_safe:
        mx, my = int(w * 0.05), int(h * 0.05)
        cv2.rectangle(canvas, (mx, my), (w - mx, h - my), (90, 90, 120), 1, cv2.LINE_AA)

    if show_center:
        cx, cy = w // 2, h // 2
        # Dashed so the guides never get mistaken for template artwork.
        for y in range(0, h, 18):
            cv2.line(canvas, (cx, y), (cx, min(y + 9, h - 1)), (0, 165, 255), 1, cv2.LINE_AA)
        for x in range(0, w, 18):
            cv2.line(canvas, (x, cy), (min(x + 9, w - 1), cy), (0, 165, 255), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"x={int(img_w / 2)}", (cx + 6, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 165, 255), 1, cv2.LINE_AA)
        cv2.putText(canvas, f"y={int(img_h / 2)}", (8, cy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 165, 255), 1, cv2.LINE_AA)


# ── Instruction panel ────────────────────────────────────────────────────────

def draw_panel(panel):
    panel[:] = (22, 22, 30)
    y = 26

    cv2.putText(panel, "DEFINE ZONES  (TL -> BR)", (10, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 220, 255), 1, cv2.LINE_AA)
    y += 8
    cv2.line(panel, (10, y), (PANEL_W - 10, y), (60, 60, 80), 1)
    y += 16

    for idx, (_, title, hint) in enumerate(ZONES):
        if idx < len(boxes):
            label_col = (110, 110, 130) if boxes[idx] == EMPTY else (0, 255, 136)
        elif idx == len(boxes):
            label_col = (255, 255, 255)
        else:
            label_col = (110, 110, 130)

        marker = ">" if idx == len(boxes) else " "
        cv2.putText(panel, f"{marker}{idx + 1:2d}. {title}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, label_col, 1, cv2.LINE_AA)
        y += 14

        if idx == len(boxes):
            cv2.putText(panel, f"      {hint}", (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.34, (150, 150, 170), 1, cv2.LINE_AA)
            y += 14

        if idx < len(boxes):
            b = boxes[idx]
            info = ("      skipped" if b == EMPTY else
                    f"      ({b[0]},{b[1]})-({b[2]},{b[3]}) {b[2]-b[0]}x{b[3]-b[1]}")
            cv2.putText(panel, info, (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.33,
                        (90, 90, 110) if b == EMPTY else (0, 190, 100), 1, cv2.LINE_AA)
            y += 14
        y += 2

    y += 6
    cv2.line(panel, (10, y), (PANEL_W - 10, y), (60, 60, 80), 1)
    y += 18

    if cur_pt1:
        cv2.putText(panel, "Now click BOTTOM-RIGHT corner", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255, 220, 0), 1, cv2.LINE_AA)
        y += 18
    elif len(boxes) < TOTAL_ZONES:
        cv2.putText(panel, f"Click TOP-LEFT of zone {len(boxes) + 1}", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 255, 255), 1, cv2.LINE_AA)
        y += 18
        partner = MIRROR_PAIRS.get(NAMES[len(boxes)])
        if partner and partner in NAMES[:len(boxes)]:
            pb = boxes[NAMES.index(partner)]
            if pb != EMPTY:
                cv2.putText(panel, f"M = mirror of {partner}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 255, 200), 1, cv2.LINE_AA)
                y += 18
    else:
        cv2.putText(panel, "All zones done — press Q", (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 136), 1, cv2.LINE_AA)
        y += 18

    y += 8
    for line in ("G centre lines   T thirds   B safe area",
                 "M mirror   C centre box   S skip",
                 "R redo last   Q/ESC finish"):
        cv2.putText(panel, line, (10, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 200, 60), 1, cv2.LINE_AA)
        y += 16

    y += 6
    done = sum(1 for b in boxes if b != EMPTY)
    cv2.putText(panel, f"Defined: {done}   Skipped: {len(boxes) - done}   of {TOTAL_ZONES}",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1, cv2.LINE_AA)


# ── Scene ────────────────────────────────────────────────────────────────────

def draw_scene(base_img):
    h, w = base_img.shape[:2]
    canvas = np.full((max(h, 320), w + PANEL_W, 3), 18, dtype=np.uint8)
    canvas[:h, :w] = base_img

    draw_guides(canvas, w, h)

    for i, box in enumerate(boxes):
        if box == EMPTY:
            continue
        x1, y1, x2, y2 = box
        col = ZONE_COLORS[i % len(ZONE_COLORS)]
        sx1, sy1 = int(x1 * scale), int(y1 * scale)
        sx2, sy2 = int(x2 * scale), int(y2 * scale)
        overlay = canvas.copy()
        cv2.rectangle(overlay, (sx1, sy1), (sx2, sy2), col, -1)
        cv2.addWeighted(overlay, 0.18, canvas, 0.82, 0, canvas)
        cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), col, 2, cv2.LINE_AA)
        cv2.putText(canvas, f"{NAMES[i]}  {x2-x1}x{y2-y1}", (sx1 + 4, sy1 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, col, 1, cv2.LINE_AA)

    if cur_pt1 and 0 <= mouse_x < w:
        col = ZONE_COLORS[len(boxes) % len(ZONE_COLORS)]
        cv2.rectangle(canvas, cur_pt1, (mouse_x, mouse_y), col, 1, cv2.LINE_AA)
        cv2.circle(canvas, cur_pt1, 5, col, -1)
        bw = abs(int((mouse_x - cur_pt1[0]) / scale))
        bh = abs(int((mouse_y - cur_pt1[1]) / scale))
        cv2.putText(canvas, f"{bw}x{bh}", (mouse_x + 10, mouse_y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, col, 1, cv2.LINE_AA)

    if 0 <= mouse_x < w:
        cv2.line(canvas, (0, mouse_y), (w - 1, mouse_y), (0, 255, 136), 1, cv2.LINE_AA)
        cv2.line(canvas, (mouse_x, 0), (mouse_x, h - 1), (0, 255, 136), 1, cv2.LINE_AA)
        rx, ry = int(mouse_x / scale), int(mouse_y / scale)
        dx = rx - img_w // 2
        side = "L" if dx < 0 else ("R" if dx > 0 else "centre")
        cv2.putText(canvas, f"({rx},{ry})  dx={abs(dx)}{'' if side == 'centre' else side}",
                    (mouse_x + 10, mouse_y - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 255, 0), 1, cv2.LINE_AA)

    draw_panel(canvas[:, w:w + PANEL_W])

    cv2.putText(canvas, f"{img_w}x{img_h}px   display {scale:.2f}x", (12, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.44, (210, 210, 210), 1, cv2.LINE_AA)
    return canvas


# ── Recording ────────────────────────────────────────────────────────────────

def record(box):
    """Append a box, echo it, and log it."""
    boxes.append(box)
    name = NAMES[len(boxes) - 1]
    if box == EMPTY:
        entry = f"{name:<20} = (0, 0, 0, 0)   # skipped"
    else:
        entry = (f"{name:<20} = {box}   "
                 f"# {box[2]-box[0]}x{box[3]-box[1]}px")
    print(entry)
    with open(LOG_FILE, 'a') as f:
        f.write(entry + '\n')


def on_mouse(event, x, y, flags, param):
    global mouse_x, mouse_y, cur_pt1
    w = param['w']

    if event == cv2.EVENT_MOUSEMOVE:
        mouse_x, mouse_y = x, y

    elif event == cv2.EVENT_LBUTTONDOWN and x < w:
        if len(boxes) >= TOTAL_ZONES:
            return
        if cur_pt1 is None:
            cur_pt1 = (x, y)
        else:
            x1 = int(min(cur_pt1[0], x) / scale)
            y1 = int(min(cur_pt1[1], y) / scale)
            x2 = int(max(cur_pt1[0], x) / scale)
            y2 = int(max(cur_pt1[1], y) / scale)
            record((x1, y1, x2, y2))
            cur_pt1 = None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global scale, cur_pt1, img_w, img_h
    global show_center, show_thirds, show_safe

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('template', nargs='?', default=DEFAULT_TEMPLATE,
                        help=f'path to a template image (default: {DEFAULT_TEMPLATE})')
    parser.add_argument('--cloudinary', metavar='KEY',
                        help='template key from CLOUDINARY_TEMPLATES (e.g. UCL, random-1)')
    args = parser.parse_args()

    if args.cloudinary:
        path = fetch_from_cloudinary(args.cloudinary)
    elif os.path.exists(args.template):
        path = args.template
    elif args.template == DEFAULT_TEMPLATE:
        # Default working copy isn't around — pull the same design from Cloudinary.
        path = fetch_from_cloudinary('UCL')
    else:
        sys.exit(f"ERROR: template not found: {args.template}")

    img = load_image(path)
    img_h, img_w = img.shape[:2]
    scale = min(1.0, MAX_H / img_h)
    disp = cv2.resize(img, (int(img_w * scale), int(img_h * scale)),
                      interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    dw = disp.shape[1]

    with open(LOG_FILE, 'w') as f:
        f.write(f"Template: {path}  ({img_w}x{img_h} px)\n" + "-" * 60 + "\n")

    print(f"\nTemplate: {path}  ({img_w}x{img_h} px, shown at {scale:.2f}x)")
    print(f"Vertical centre x={img_w // 2}   horizontal centre y={img_h // 2}")
    print("Click TOP-LEFT then BOTTOM-RIGHT of each zone. S skips, M mirrors, Q finishes.\n")

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, on_mouse, {'w': dw})

    while True:
        cv2.imshow(WINDOW_NAME, draw_scene(disp))
        key = cv2.waitKey(16) & 0xFF

        if key in (ord('q'), 27):
            break
        elif key == ord('g'):
            show_center = not show_center
        elif key == ord('t'):
            show_thirds = not show_thirds
        elif key == ord('b'):
            show_safe = not show_safe
        elif key == ord('s') and len(boxes) < TOTAL_ZONES:
            cur_pt1 = None
            record(EMPTY)
        elif key == ord('r') and boxes:
            removed = boxes.pop()
            cur_pt1 = None
            print(f"Removed {NAMES[len(boxes)]} {removed} — redo it.")
        elif key == ord('m') and len(boxes) < TOTAL_ZONES:
            partner = MIRROR_PAIRS.get(NAMES[len(boxes)])
            if partner and partner in NAMES[:len(boxes)]:
                px1, py1, px2, py2 = boxes[NAMES.index(partner)]
                if (px1, py1, px2, py2) != EMPTY:
                    cur_pt1 = None
                    record((img_w - px2, py1, img_w - px1, py2))
                else:
                    print(f"{partner} was skipped — nothing to mirror.")
            else:
                print(f"{NAMES[len(boxes)]} has no mirror partner yet.")
        elif key == ord('c') and boxes and boxes[-1] != EMPTY:
            x1, y1, x2, y2 = boxes[-1]
            half = (x2 - x1) // 2
            cx = img_w // 2
            boxes[-1] = (cx - half, y1, cx + half, y2)
            print(f"Centred {NAMES[len(boxes) - 1]} -> {boxes[-1]}")

        if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
            break

    cv2.destroyAllWindows()

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "─" * 68)
    print("Paste into the BOUNDING BOXES block at the top of scorecard.py:")
    print("─" * 68)
    for i, name in enumerate(NAMES):
        box = boxes[i] if i < len(boxes) else EMPTY
        note = '' if box != EMPTY else '   # not set — renderer skips this zone'
        print(f"{name:<20} = {box}{note}")

    if len(boxes) < TOTAL_ZONES:
        print(f"\n⚠  {TOTAL_ZONES - len(boxes)} zone(s) never reached — "
              f"run again to finish them.")
    print(f"\nLog: {LOG_FILE}")


if __name__ == '__main__':
    main()
