"""
heartopia_canvas_detect.py  v3
═══════════════════════════════════════════════════════════════════════════════
Automatic canvas detection for Heartopia Auto Painter.

HOW IT WORKS  (two-pass column/row density)
────────────────────────────────────────────
The empty Heartopia canvas has a diagonal stripe texture.  Every stripe pixel
has brightness > 243, while the surrounding beige padding sits at ~232.

Pass 1 — find X bounds:
    Compute the fraction of BRIGHT pixels in each screen column.
    Canvas columns hit ~0.41 (41 % of their height is stripe-bright).
    Smooth with a 20-px window, threshold at 0.25 → contiguous X block.

Pass 2 — find Y bounds:
    Restrict to the X block found above, then repeat for rows.
    Row density inside those columns is ~0.65-0.78 within the canvas,
    drops to 0 outside it.  Threshold at 0.25 → Y block.

Result on the provided screenshot:  x=638, y=261, w=681, h=685
Manual measurement:                  x=640, y=261, w=676, h=684   (≤3 px error)

LIVE INDICATOR
──────────────
After detection, an overlay window appears showing:
  Cyan  border  = detected grid boundary
  Green lines   = every Nth cell line
  Orange dots   = corner + centre cells
  White text    = dimensions, scale, method

Press any key / click / wait 4 s to dismiss and start painting.

PUBLIC API
──────────
    result = detect_canvas(gw, gh, show_preview=True, method="auto")

    result keys:
        snip_x, snip_y, snip_w, snip_h  — grid area on screen (px)
        scale                            — screen_px / design_px
        method                           — "stripe" | "overlay" | "manual"
        cell_to_screen                   — callable (x,y) → (screen_x, screen_y)
"""

import os, sys, math

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    from PIL import Image, ImageGrab, ImageEnhance
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import pyautogui
    _HAS_PYAUTOGUI = True
except ImportError:
    _HAS_PYAUTOGUI = False

# ── Design constants (JS canvas.js) ───────────────────────────────────────────
CANVAS_DESIGN_W = 1100
CANVAS_DESIGN_H = 630

GRID_DIMENSIONS = {
    "16:9": [(30, 18),  (50, 28),  (100, 56),  (150, 84)],
    "4:3":  [(30, 24),  (50, 38),  (100, 76),  (150, 114)],
    "1:1":  [(30, 30),  (50, 50),  (100, 100), (150, 150)],
    "3:4":  [(24, 30),  (38, 50),  (76, 100),  (114, 150)],
    "9:16": [(18, 30),  (28, 50),  (56, 100),  (84, 150)],
}
DETAIL_NAMES = ["small", "medium", "large", "extra large"]

# Stripe detection parameters
_STRIPE_THRESHOLD = 243   # pixel brightness threshold (0-255)
_DENSITY_THRESHOLD = 0.25 # fraction of column/row that must be bright
_SMOOTH_WIN = 20          # smoothing window in pixels


# ══════════════════════════════════════════════════════════════════════════════
# 1.  Two-pass stripe detection
# ══════════════════════════════════════════════════════════════════════════════

def _two_pass_detect(gray):
    """
    Two-pass column/row density detection.
    gray : 2-D float array (mean of RGB channels)
    Returns (lx, ty, rw, rh) or None.
    """
    bright = (gray > _STRIPE_THRESHOLD).astype(float)
    h, w = gray.shape

    # ── Pass 1: X bounds ──────────────────────────────────────────────────────
    col_density = bright.mean(axis=0)
    col_smooth  = np.convolve(col_density, np.ones(_SMOOTH_WIN) / _SMOOTH_WIN,
                              mode="same")
    high_cols = np.where(col_smooth > _DENSITY_THRESHOLD)[0]
    if len(high_cols) < 10:
        return None
    lx = int(high_cols[0])
    rx = int(high_cols[-1])

    # ── Pass 2: Y bounds (restricted to canvas columns) ───────────────────────
    row_density = bright[:, lx:rx].mean(axis=1)
    high_rows   = np.where(row_density > _DENSITY_THRESHOLD)[0]
    if len(high_rows) < 10:
        return None
    ty = int(high_rows[0])
    by = int(high_rows[-1])

    rw = rx - lx
    rh = by - ty
    if rw < 20 or rh < 20:
        return None

    return lx, ty, rw, rh


def detect_canvas_stripe(gw, gh, screenshot=None, verbose=True):
    """
    Auto-detect the grid area using the diagonal-stripe brightness pattern.
    Returns result dict or None on failure.
    """
    if not (_HAS_NUMPY and _HAS_PIL):
        if verbose:
            print("[stripe] Needs numpy + Pillow — skipping.")
        return None

    if screenshot is None:
        try:
            screenshot = ImageGrab.grab()
        except Exception as e:
            if verbose:
                print(f"[stripe] Screenshot failed: {e}")
            return None

    arr  = np.array(screenshot)
    gray = arr[:, :, :3].mean(axis=2).astype(float)

    region = _two_pass_detect(gray)
    if region is None:
        if verbose:
            print("[stripe] Stripe region not found "
                  "(canvas may be fully painted or not on screen).")
        return None

    lx, ty, rw, rh = region

    # Estimate scale from measured vs expected grid size
    ps_design     = min(CANVAS_DESIGN_W / gw, CANVAS_DESIGN_H / gh)
    grid_design_w = ps_design * gw
    grid_design_h = ps_design * gh
    scale = ((rw / grid_design_w) + (rh / grid_design_h)) / 2

    if verbose:
        print(f"[stripe]  Found  : x={lx}  y={ty}  w={rw}  h={rh}")
        print(f"          Expected grid design: {grid_design_w:.0f}×{grid_design_h:.0f} px")
        print(f"          Scale  : {scale:.4f}x")

    return {"snip_x": lx, "snip_y": ty, "snip_w": rw, "snip_h": rh,
            "scale": scale, "method": "stripe"}


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Overlay drag (fallback)
# ══════════════════════════════════════════════════════════════════════════════

def detect_canvas_overlay(gw, gh, verbose=True):
    """
    Full-screen drag overlay — user drags over the STRIPED grid area.
    Returns result dict or None if cancelled.
    """
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        if verbose:
            print("[overlay] Needs tkinter + Pillow.")
        return None

    result    = {}
    screenshot = ImageGrab.grab()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost",    True)
    root.overrideredirect(True)

    cv = tk.Canvas(root, cursor="crosshair", bg="grey15", highlightthickness=0)
    cv.pack(fill=tk.BOTH, expand=True)

    bg    = ImageEnhance.Brightness(screenshot).enhance(0.4)
    bg_tk = ImageTk.PhotoImage(bg)
    cv.create_image(0, 0, anchor="nw", image=bg_tk)

    state   = {"x0": 0, "y0": 0, "down": False}
    rect_id = cv.create_rectangle(0, 0, 0, 0, outline="#00FFFF", width=2)
    info_id = cv.create_text(
        8, 8, anchor="nw", fill="white",
        font=("Consolas", 12, "bold"),
        text=(f"Drag over the STRIPED grid area — skip the beige padding  "
              f"│  Grid: {gw}×{gh}  │  ESC = cancel")
    )

    def press(e):
        state.update(x0=e.x, y0=e.y, down=True)
        cv.coords(rect_id, e.x, e.y, e.x, e.y)

    def drag(e):
        if not state["down"]: return
        cv.coords(rect_id, state["x0"], state["y0"], e.x, e.y)
        sw2 = abs(e.x - state["x0"]); sh2 = abs(e.y - state["y0"])
        cv.itemconfig(info_id,
            text=(f"({min(state['x0'],e.x)},{min(state['y0'],e.y)})  "
                  f"{sw2}×{sh2} px  │  "
                  f"cell≈{sw2/gw:.1f}×{sh2/gh:.1f} px  │  ESC=cancel"))

    def release(e):
        state["down"] = False
        x0  = min(state["x0"], e.x);  y0  = min(state["y0"], e.y)
        sw2 = abs(e.x - state["x0"]); sh2 = abs(e.y - state["y0"])
        if sw2 > 10 and sh2 > 10:
            result["rect"] = (x0, y0, sw2, sh2)
        root.destroy()

    cv.bind("<ButtonPress-1>",    press)
    cv.bind("<B1-Motion>",        drag)
    cv.bind("<ButtonRelease-1>",  release)
    root.bind("<Escape>",         lambda e: root.destroy())
    root.mainloop()

    if "rect" not in result:
        return None

    lx, ty, rw, rh = result["rect"]
    ps    = min(CANVAS_DESIGN_W / gw, CANVAS_DESIGN_H / gh)
    scale = rw / (ps * gw)

    if verbose:
        print(f"[overlay] x={lx}  y={ty}  w={rw}  h={rh}  scale≈{scale:.4f}")
    return {"snip_x": lx, "snip_y": ty, "snip_w": rw, "snip_h": rh,
            "scale": scale, "method": "overlay"}


# ══════════════════════════════════════════════════════════════════════════════
# 3.  Manual two-corner fallback
# ══════════════════════════════════════════════════════════════════════════════

def _detect_canvas_manual(gw, gh, verbose=True):
    if not _HAS_PYAUTOGUI:
        raise RuntimeError("pyautogui required for manual mode.")

    def _get(prompt):
        print(prompt)
        input("  → Press ENTER while hovering…")
        p = pyautogui.position()
        print(f"     ({p.x}, {p.y})")
        return p.x, p.y

    print("[manual] Hover TOP-LEFT corner of the STRIPED area.")
    x1, y1 = _get("")
    print("[manual] Hover BOTTOM-RIGHT corner of the STRIPED area.")
    x2, y2 = _get("")

    rw = abs(x2 - x1); rh = abs(y2 - y1)
    if rw < 10 or rh < 10:
        raise ValueError(f"Selection too small: {rw}×{rh}")

    ps    = min(CANVAS_DESIGN_W / gw, CANVAS_DESIGN_H / gh)
    scale = rw / (ps * gw)
    return {"snip_x": min(x1,x2), "snip_y": min(y1,y2),
            "snip_w": rw,         "snip_h": rh,
            "scale": scale,       "method": "manual"}


# ══════════════════════════════════════════════════════════════════════════════
# 4.  Cell → screen coordinate builder
# ══════════════════════════════════════════════════════════════════════════════

def make_cell_to_screen(grid_rect, gw, gh):
    """
    Build  cell_to_screen(x, y) → (screen_x, screen_y).

    snip_x/y/w/h  =  the actual painted-grid area on screen.
    Clicks the CENTRE of each cell.
    """
    bx = grid_rect["snip_x"]; by = grid_rect["snip_y"]
    rw = grid_rect["snip_w"]; rh = grid_rect["snip_h"]

    cell_w = rw / gw
    cell_h = rh / gh

    xs = [round(bx + x * cell_w + cell_w / 2) for x in range(gw)]
    ys = [round(by + y * cell_h + cell_h / 2) for y in range(gh)]

    dup_x = len(xs) - len(set(xs))
    dup_y = len(ys) - len(set(ys))
    if dup_x or dup_y:
        print(f"  ⚠  {dup_x} duplicate X, {dup_y} duplicate Y — "
              "canvas too small, try zooming in.")
    else:
        print(f"  ✓  {gw}×{gh} = {gw*gh} cells  │  "
              f"cell {cell_w:.2f}×{cell_h:.2f} px  │  "
              f"X {xs[0]}→{xs[-1]}  Y {ys[0]}→{ys[-1]}")

    def cell_to_screen(x, y):
        return xs[x], ys[y]

    return cell_to_screen


# ══════════════════════════════════════════════════════════════════════════════
# 5.  Live visual indicator
# ══════════════════════════════════════════════════════════════════════════════

def show_detection_preview(grid_rect, gw, gh, duration=4.0):
    """
    Semi-transparent on-screen overlay showing the detected grid.
    Auto-closes after `duration` seconds, or on click / ESC.
    """
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        print("  (preview skipped — tkinter not available)")
        return

    bx = grid_rect["snip_x"]; by = grid_rect["snip_y"]
    rw = grid_rect["snip_w"]; rh = grid_rect["snip_h"]
    scale  = grid_rect.get("scale",  1.0)
    method = grid_rect.get("method", "?")
    cell_w = rw / gw
    cell_h = rh / gh

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost",    True)
    root.overrideredirect(True)
    root.attributes("-alpha", 0.82)
    root.configure(bg="black")
    root.wm_attributes("-transparentcolor", "black")

    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill=tk.BOTH, expand=True)

    # ── Cyan grid border ──────────────────────────────────────────────────────
    cv.create_rectangle(bx-2, by-2, bx+rw+2, by+rh+2,
                        outline="#00FFFF", width=4)

    # ── Green grid lines (≤20 per axis for readability) ───────────────────────
    step_x = max(1, gw  // 20)
    step_y = max(1, gh // 20)
    for i in range(0, gw+1, step_x):
        x = round(bx + i * cell_w)
        cv.create_line(x, by, x, by+rh, fill="#00DD00", width=1)
    for j in range(0, gh+1, step_y):
        y = round(by + j * cell_h)
        cv.create_line(bx, y, bx+rw, y, fill="#00DD00", width=1)

    # ── Orange dots at corners + centre ───────────────────────────────────────
    dot_r = max(4, round(min(cell_w, cell_h) * 0.45))
    for (ci, cj) in [(0,0),(gw-1,0),(0,gh-1),(gw-1,gh-1),(gw//2,gh//2)]:
        cx_ = round(bx + ci * cell_w + cell_w / 2)
        cy_ = round(by + cj * cell_h + cell_h / 2)
        cv.create_oval(cx_-dot_r, cy_-dot_r, cx_+dot_r, cy_+dot_r,
                       fill="#FF8800", outline="#FFEE00", width=2)

    # ── Info label ────────────────────────────────────────────────────────────
    label = (
        f"✓  Heartopia Canvas Detected  [{method.upper()}]\n"
        f"Grid {gw}×{gh}  |  Pos ({bx},{by})  Size {rw}×{rh}px  |  "
        f"Cell {cell_w:.1f}×{cell_h:.1f}px  |  Scale {scale:.4f}x\n"
        f"Click or wait {int(duration)}s to start painting…"
    )
    label_y = max(8, by - 62)
    cv.create_text(bx+2, label_y+2, anchor="nw",
                   text=label, fill="#000000", font=("Consolas", 10, "bold"))
    cv.create_text(bx,   label_y,   anchor="nw",
                   text=label, fill="#FFFFFF", font=("Consolas", 10, "bold"))

    root.after(int(duration * 1000), root.destroy)
    root.bind("<Button-1>", lambda e: root.destroy())
    root.bind("<Escape>",   lambda e: root.destroy())
    root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
# 6.  Main entry-point
# ══════════════════════════════════════════════════════════════════════════════

def detect_canvas(gw, gh,
                  method       = "auto",
                  show_preview = True,
                  screenshot   = None,
                  verbose      = True):
    """
    Detect the Heartopia painting grid on screen.

    Parameters
    ──────────
    gw, gh        : grid dimensions (e.g. 100, 100 for 1:1 Large)
    method        : "auto" | "stripe" | "overlay" | "manual"
                    "auto" tries stripe → overlay automatically
    show_preview  : show the live overlay indicator after detection
    screenshot    : PIL Image to reuse (None = grab fresh)
    verbose       : print progress

    Returns
    ───────
    dict {
      snip_x, snip_y, snip_w, snip_h,   ← grid area on screen
      scale,                              ← screen px / design px
      method,                             ← method that succeeded
      cell_to_screen,                     ← callable (x,y)→(sx,sy)
    }
    """
    if verbose:
        print(f"\n{'═'*60}")
        print(f"  Heartopia Canvas Detection  [method={method}]")
        print(f"  Grid : {gw}×{gh}  ({gw*gh} cells)")
        print(f"{'═'*60}")

    result = None

    if method in ("auto", "stripe"):
        result = detect_canvas_stripe(gw, gh,
                                      screenshot=screenshot,
                                      verbose=verbose)

    if result is None and method in ("auto", "overlay"):
        print("\n  Stripe auto-detect failed.")
        print("  Opening drag-selection — draw over the STRIPED area only.")
        result = detect_canvas_overlay(gw, gh, verbose=verbose)

    if result is None and method == "manual":
        result = _detect_canvas_manual(gw, gh, verbose=verbose)

    if result is None:
        raise RuntimeError(
            "Canvas detection failed.\n"
            "Fixes:\n"
            "  • The canvas must show diagonal stripes (some cells unpainted)\n"
            "  • Use method='overlay' to select manually\n"
            "  • Make sure the game window is fully visible on screen"
        )

    print()
    result["cell_to_screen"] = make_cell_to_screen(result, gw, gh)

    if verbose:
        print(f"\n  ✓  Ready  [{result['method']}]  "
              f"grid ({result['snip_x']},{result['snip_y']})  "
              f"{result['snip_w']}×{result['snip_h']} px  "
              f"scale {result['scale']:.4f}x")

    if show_preview:
        print("\n  Preview overlay — click or wait 4 s to start…")
        show_detection_preview(result, gw, gh, duration=4.0)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 7.  Grid / detail selection helper
# ══════════════════════════════════════════════════════════════════════════════

def select_ratio_and_detail():
    """Interactive CLI prompt. Returns (ratio, detail_idx, gw, gh)."""
    ratio_list = list(GRID_DIMENSIONS.keys())
    print("\nCanvas sizes:")
    for ratio in ratio_list:
        row = "  |  ".join(
            f"[{j}] {DETAIL_NAMES[j]} "
            f"{GRID_DIMENSIONS[ratio][j][0]}×{GRID_DIMENSIONS[ratio][j][1]}"
            for j in range(4))
        print(f"  {ratio:5s}  →  {row}")

    ratio = input(f"\nAspect ratio ({', '.join(ratio_list)}) [1:1]: ").strip() or "1:1"
    if ratio not in GRID_DIMENSIONS:
        ratio = "1:1"

    for j in range(4):
        gw2, gh2 = GRID_DIMENSIONS[ratio][j]
        ps = min(CANVAS_DESIGN_W / gw2, CANVAS_DESIGN_H / gh2)
        print(f"  [{j}] {DETAIL_NAMES[j]:12s}  {gw2:3d}×{gh2:3d}  "
              f"({gw2*gh2:6d} cells,  cell≈{ps:.2f} design-px)")

    di = input("Detail (0-3) [1]: ").strip() or "1"
    try:
        detail_idx = int(di)
        if detail_idx not in range(4):
            detail_idx = 1
    except ValueError:
        detail_idx = {n: i for i, n in enumerate(DETAIL_NAMES)}.get(di.lower(), 1)

    gw, gh = GRID_DIMENSIONS[ratio][detail_idx]
    print(f"→  {ratio}  {DETAIL_NAMES[detail_idx]} : {gw}×{gh}  ({gw*gh} cells)\n")
    return ratio, detail_idx, gw, gh


# ══════════════════════════════════════════════════════════════════════════════
# 8.  Stand-alone test / demo
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== Heartopia Canvas Detector  v3  ===\n")
    ratio, detail_idx, gw, gh = select_ratio_and_detail()

    print("Detection method:")
    print("  [1] auto    — stripe → overlay fallback (recommended)")
    print("  [2] stripe  — brightness auto-detect (canvas must have stripes)")
    print("  [3] overlay — drag-select the stripe area manually")
    print("  [4] manual  — click two corners (requires pyautogui)")
    m = input("Choice [1]: ").strip() or "1"
    method = {"1":"auto","2":"stripe","3":"overlay","4":"manual"}.get(m, "auto")

    canvas = detect_canvas(gw, gh, method=method, show_preview=True)

    print("\nSample coordinates:")
    for (x, y) in [(0, 0), (gw//2, gh//2), (gw-1, gh-1)]:
        sx, sy = canvas["cell_to_screen"](x, y)
        print(f"  cell ({x:3d},{y:3d}) → screen ({sx},{sy})")

    print("\nDone.")
