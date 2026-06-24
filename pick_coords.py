"""
pick_coords.py — Bounding-box picker for the scorecard template.
Click TOP-LEFT then BOTTOM-RIGHT for each zone to define a rectangle.

Zones to define (in order):
  1.  Group / Stage label     ("GROUP H | GROUP STAGE")
  2.  Home team crest         (left flag/crest box)
  3.  Away team crest         (right flag/crest box)
  4.  Home team name          (left team name text)
  5.  Away team name          (right team name text)
  6.  Home team score         (large left score digit)
  7.  Away team score         (large right score digit)
  8.  Stadium name            (bottom centre text)
  9.  Home team scorers       (goal-scorer lines, left side)
  10. Away team scorers       (goal-scorer lines, right side)

REQUIREMENTS:
  pip install opencv-python
"""

import os
import cv2
import numpy as np

TEMPLATE_PATH = "Full time template.jpg"
LOG_FILE      = "coords_log.txt"
MAX_H         = 900
PANEL_W       = 430
WINDOW_NAME   = "Box Picker — click TOP-LEFT then BOTTOM-RIGHT of each zone"

# ── Zone definitions ────────────────────────────────────────────────────────

ZONES = [
    "1. Group / Stage label\n   e.g. 'GROUP H | GROUP STAGE'",
    "2. Home team crest\n   (left flag / badge box)",
    "3. Away team crest\n   (right flag / badge box)",
    "4. Home team name\n   (left team name text)",
    "5. Away team name\n   (right team name text)",
    "6. Home team score\n   (large left digit)",
    "7. Away team score\n   (large right digit)",
    "8. Stadium name\n   (bottom centre text)",
    "9. Home team scorers\n   (goal lines, left side)",
    "10. Away team scorers\n   (goal lines, right side)",
]

ZONE_COLORS = [
    (0,   220, 255),   # cyan       — group/stage
    (255, 160,   0),   # amber      — home crest
    (255,  60, 200),   # pink       — away crest
    (0,   200, 255),   # azure      — home name
    (255, 120, 220),   # rose       — away name
    (0,   255, 128),   # green      — home score
    (80,  180, 255),   # sky blue   — away score
    (255, 255,  60),   # yellow     — stadium
    (200, 100, 255),   # violet     — home scorers
    (60,  220, 160),   # teal       — away scorers
]

SUMMARY_NAMES = [
    "GROUP_STAGE_BOX",
    "HOME_CREST_BOX",
    "AWAY_CREST_BOX",
    "HOME_NAME_BOX",
    "AWAY_NAME_BOX",
    "HOME_SCORE_BOX",
    "AWAY_SCORE_BOX",
    "STADIUM_BOX",
    "HOME_SCORERS_BOX",
    "AWAY_SCORERS_BOX",
]

TOTAL_ZONES = len(ZONES)

# ── State ────────────────────────────────────────────────────────────────────

boxes   = []      # finished boxes: (x1, y1, x2, y2)
cur_pt1 = None    # first click of the current box
mouse_x = 0
mouse_y = 0
scale   = 1.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_image(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Template not found: {path}")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        bgr   = img[:, :, :3].astype(np.float32)
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        bg    = np.full_like(bgr, 28.0)
        img   = (bgr * alpha + bg * (1 - alpha)).astype(np.uint8)
    else:
        img = img[:, :, :3]
    return img


def draw_panel(panel):
    """Render the right-hand instruction panel."""
    panel[:] = (22, 22, 30)
    y = 26

    cv2.putText(panel, "DEFINE ZONES  (TL -> BR)",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 220, 255), 1, cv2.LINE_AA)
    y += 8
    cv2.line(panel, (10, y), (PANEL_W - 10, y), (60, 60, 80), 1)
    y += 14

    for idx, z in enumerate(ZONES):
        col = ZONE_COLORS[idx % len(ZONE_COLORS)]
        if idx < len(boxes):
            label_col = (0, 255, 136)       # done  ✓
        elif idx == len(boxes):
            label_col = (255, 255, 255)     # active
        else:
            label_col = (110, 110, 130)     # pending

        for line in z.split('\n'):
            cv2.putText(panel, line.strip(), (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, label_col, 1, cv2.LINE_AA)
            y += 15

        if idx < len(boxes):
            b = boxes[idx]
            info = f"  ({b[0]},{b[1]}) -> ({b[2]},{b[3]})  {b[2]-b[0]}x{b[3]-b[1]}px"
            cv2.putText(panel, info, (10, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 190, 100), 1, cv2.LINE_AA)
            y += 14
        y += 5

    # Current action hint
    y += 6
    cv2.line(panel, (10, y), (PANEL_W - 10, y), (60, 60, 80), 1)
    y += 14

    if cur_pt1:
        n = len(boxes) + 1
        cv2.putText(panel, f"Zone {n}: pt1 set at ({int(cur_pt1[0]/scale)},{int(cur_pt1[1]/scale)})",
                    (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255, 220, 0), 1, cv2.LINE_AA)
        y += 16
        cv2.putText(panel, "Now click BOTTOM-RIGHT corner",
                    (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (255, 220, 0), 1, cv2.LINE_AA)
    else:
        next_idx = len(boxes)
        if next_idx < TOTAL_ZONES:
            cv2.putText(panel, f"Click TOP-LEFT of Zone {next_idx + 1}",
                        (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255, 255, 255), 1, cv2.LINE_AA)
        else:
            cv2.putText(panel, "All zones done!  Press Q to finish.",
                        (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (0, 255, 136), 1, cv2.LINE_AA)

    y += 32
    cv2.putText(panel, "R = redo last box   Q / ESC = quit",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (255, 200, 60), 1, cv2.LINE_AA)
    y += 16
    cv2.putText(panel, f"Progress: {len(boxes)} / {TOTAL_ZONES}",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 180, 180), 1, cv2.LINE_AA)
    y += 14
    cv2.putText(panel, f"Log: {LOG_FILE}",
                (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (130, 130, 150), 1, cv2.LINE_AA)


def draw_scene(base_img):
    h, w = base_img.shape[:2]
    canvas = np.full((max(h, 300), w + PANEL_W, 3), 18, dtype=np.uint8)
    canvas[:h, :w] = base_img

    # Completed boxes
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        col = ZONE_COLORS[i % len(ZONE_COLORS)]
        sx1, sy1 = int(x1 * scale), int(y1 * scale)
        sx2, sy2 = int(x2 * scale), int(y2 * scale)
        overlay = canvas.copy()
        cv2.rectangle(overlay, (sx1, sy1), (sx2, sy2), col, -1)
        cv2.addWeighted(overlay, 0.18, canvas, 0.82, 0, canvas)
        cv2.rectangle(canvas, (sx1, sy1), (sx2, sy2), col, 2, cv2.LINE_AA)
        label = SUMMARY_NAMES[i] if i < len(SUMMARY_NAMES) else f"Z{i+1}"
        cv2.putText(canvas, f"{label}  {x2-x1}x{y2-y1}",
                    (sx1 + 4, sy1 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, col, 1, cv2.LINE_AA)

    # In-progress box
    if cur_pt1 and 0 <= mouse_x < w:
        col = ZONE_COLORS[len(boxes) % len(ZONE_COLORS)]
        x1s, y1s = cur_pt1
        cv2.rectangle(canvas, (x1s, y1s), (mouse_x, mouse_y), col, 1, cv2.LINE_AA)
        cv2.circle(canvas, (x1s, y1s), 5, col, -1)

    # Crosshair
    if 0 <= mouse_x < w:
        cv2.line(canvas, (0, mouse_y), (w - 1, mouse_y), (0, 255, 136), 1, cv2.LINE_AA)
        cv2.line(canvas, (mouse_x, 0), (mouse_x, h - 1), (0, 255, 136), 1, cv2.LINE_AA)
        rx, ry = int(mouse_x / scale), int(mouse_y / scale)
        cv2.putText(canvas, f"({rx},{ry})", (mouse_x + 10, mouse_y - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 255, 0), 1, cv2.LINE_AA)

    # Side panel
    draw_panel(canvas[:, w:w + PANEL_W])

    # Status bar
    cv2.putText(canvas, f"Zones: {len(boxes)}/{TOTAL_ZONES}   Scale: {scale:.2f}x",
                (12, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (210, 210, 210), 1, cv2.LINE_AA)

    return canvas


def on_mouse(event, x, y, flags, param):
    global mouse_x, mouse_y, cur_pt1, boxes
    w = param['w']

    if event == cv2.EVENT_MOUSEMOVE:
        mouse_x, mouse_y = x, y

    elif event == cv2.EVENT_LBUTTONDOWN and x < w:
        if len(boxes) >= TOTAL_ZONES:
            return
        if cur_pt1 is None:
            cur_pt1 = (x, y)
        else:
            x1s, y1s = cur_pt1
            x1 = int(min(x1s, x) / scale)
            y1 = int(min(y1s, y) / scale)
            x2 = int(max(x1s, x) / scale)
            y2 = int(max(y1s, y) / scale)
            boxes.append((x1, y1, x2, y2))
            n    = len(boxes)
            name = SUMMARY_NAMES[n - 1]
            entry = f"{name:<22} = ({x1}, {y1}, {x2}, {y2})   # {x2-x1}x{y2-y1}px"
            print(entry)
            with open(LOG_FILE, 'a') as f:
                f.write(entry + '\n')
            cur_pt1 = None


def main():
    global scale, cur_pt1, boxes

    if not os.path.exists(TEMPLATE_PATH):
        print(f"ERROR: Template not found at '{TEMPLATE_PATH}'")
        return

    img = load_image(TEMPLATE_PATH)
    h, w = img.shape[:2]
    scale = min(1.0, MAX_H / h)
    dw, dh = int(w * scale), int(h * scale)
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    disp = cv2.resize(img, (dw, dh), interpolation=interp)

    boxes, cur_pt1 = [], None

    with open(LOG_FILE, 'w') as f:
        f.write(f"Template: {TEMPLATE_PATH}  ({w}x{h} px)\n" + "-" * 60 + "\n")

    print(f"\nTemplate loaded ({w}x{h} px, displayed at {scale:.2f}x).")
    print(f"Define {TOTAL_ZONES} bounding boxes — click TOP-LEFT then BOTTOM-RIGHT.\n")

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, on_mouse, {'w': dw})

    while True:
        cv2.imshow(WINDOW_NAME, draw_scene(disp))
        key = cv2.waitKey(16) & 0xFF
        if key in (ord('q'), 27):
            break
        if key == ord('r') and boxes:
            removed = boxes.pop()
            cur_pt1 = None
            print(f"Removed last box: {removed} — redo it.")
        if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
            break

    cv2.destroyAllWindows()

    # ── Final summary ─────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("SUMMARY — paste these constants into your scorecard builder:")
    print("─" * 60)
    for i, (x1, y1, x2, y2) in enumerate(boxes):
        name = SUMMARY_NAMES[i] if i < len(SUMMARY_NAMES) else f"ZONE_{i+1}_BOX"
        print(f"{name:<22} = ({x1}, {y1}, {x2}, {y2})")

    if len(boxes) < TOTAL_ZONES:
        missing = TOTAL_ZONES - len(boxes)
        print(f"\n⚠  {missing} zone(s) not defined — run again to complete them.")

    print(f"\nFull log saved to: {LOG_FILE}")


if __name__ == '__main__':
    main()