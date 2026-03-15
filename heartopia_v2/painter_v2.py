"""
Heartopia Auto Painter  v6
──────────────────────────
Changes from v5:
  ✓ Canvas detection is AUTOMATIC via diagonal-stripe brightness analysis.
    No manual drag needed.  Falls back to overlay drag if canvas is fully painted.
  ✓ DOUBLE PAINT — every cell is clicked twice (slightly offset second click)
    so missed cells get a second chance.
  ✓ PAUSE / RESUME — press F10 to pause.  A small dialog appears; press F10
    again or click Resume to continue from exactly where it left off.
  ✓ STOP — press F12 to abort completely (unchanged from v5).

REQUIREMENTS
────────────
  pip install pyautogui pillow numpy keyboard
"""

import pyautogui
from PIL import Image, ImageGrab, ImageEnhance, ImageDraw
import time, sys, os, math, threading

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

pyautogui.FAILSAFE = False
pyautogui.PAUSE    = 0.0

# ── Timing ────────────────────────────────────────────────────────────────────
DELAY_AFTER_COLOR_SELECT = 0.10   # was 0.15 — palette click still needs to register
CLICK_HOLD_DURATION      = 0.04
CANVAS_CLICK_DELAY       = 0.01
PAINT_CLICK_HOLD         = 0.012  # 1 browser frame (16 ms) is enough; 12 ms is safe
PAINT_CLICK_DELAY        = 0.008  # short settle after mouseUp
DOUBLE_PAINT_GAP         = 0.010  # mouseUp→mouseDown gap; browser sees two distinct clicks
SCROLL_HOLD_BEFORE_DRAG  = 0.2
SCROLL_DRAG_STEPS        = 15
SCROLL_DRAG_DURATION     = 0.6
SCROLL_DELAY_AFTER       = 0.6
PALETTE_MOVE_PAUSE       = 0.1
PALETTE_CLICK_HOLD       = 0.12
STOP_HOTKEY              = "f12"
PAUSE_HOTKEY             = "f10"
CALIBRATION_FILE         = "heartopia_calibration.json"

# ── Canvas design constants (JS source) ───────────────────────────────────────
CANVAS_W = 1100
CANVAS_H = 630

# ── Stripe detection constants ────────────────────────────────────────────────
_STRIPE_THRESHOLD  = 243
_DENSITY_THRESHOLD = 0.25
_SMOOTH_WIN        = 20

# ── Global state ──────────────────────────────────────────────────────────────
stop_painting  = False
pause_painting = False
_pause_event   = threading.Event()
_pause_event.set()   # set = running; clear = paused

def set_stop():
    global stop_painting
    stop_painting = True

def toggle_pause():
    """Called from the keyboard hotkey thread — only sets flags, never touches tkinter."""
    global pause_painting
    pause_painting = not pause_painting
    if pause_painting:
        _pause_event.clear()
    else:
        _pause_event.set()

def _show_pause_dialog():
    """
    Must be called from the MAIN thread only.
    Blocks until the user resumes (button or F10).
    """
    try:
        import tkinter as tk
    except ImportError:
        # No tkinter — just print and wait for hotkey
        print(f"\n⏸  PAUSED — press {PAUSE_HOTKEY.upper()} to resume…")
        _pause_event.wait()
        return

    def _resume():
        global pause_painting
        pause_painting = False
        _pause_event.set()
        try: root.destroy()
        except Exception: pass

    root = tk.Tk()
    root.title("Heartopia — PAUSED")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.configure(bg="#1e1e2e", padx=24, pady=18)

    tk.Label(root, text="⏸  Painting is paused",
             bg="#1e1e2e", fg="#cdd6f4",
             font=("Segoe UI", 13, "bold")).pack(pady=(0, 10))
    tk.Label(root,
             text=f"Press  {PAUSE_HOTKEY.upper()}  or click Resume to continue.",
             bg="#1e1e2e", fg="#a6adc8",
             font=("Segoe UI", 10)).pack(pady=(0, 14))
    tk.Button(root, text="▶  Resume", command=_resume,
              bg="#89b4fa", fg="#1e1e2e",
              font=("Segoe UI", 11, "bold"),
              relief="flat", padx=16, pady=6,
              cursor="hand2").pack()

    root.bind("<Escape>", lambda e: _resume())

    # Centre on screen
    root.update_idletasks()
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    w  = root.winfo_width();        h  = root.winfo_height()
    root.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    # Poll so the dialog closes if F10 was pressed again before clicking Resume
    def _poll():
        if not pause_painting:
            try: root.destroy()
            except Exception: pass
            return
        root.after(100, _poll)
    root.after(100, _poll)
    root.mainloop()

def _wait_if_paused():
    """
    Called from the main painting thread before every click.
    If paused, shows the tkinter dialog HERE (main thread) then blocks
    until resumed.  This avoids the Tcl_AsyncDelete threading crash.
    """
    if pause_painting:
        _show_pause_dialog()   # runs in main thread — safe
        _pause_event.wait()    # in case hotkey already cleared the flag

# ── Grid dimensions ── exact copy of JS gridDimensions ───────────────────────
GRID_DIMENSIONS = {
    "16:9": [(30,18),  (50,28),   (100,56),  (150,84)],
    "4:3":  [(30,24),  (50,38),   (100,76),  (150,114)],
    "1:1":  [(30,30),  (50,50),   (100,100), (150,150)],
    "3:4":  [(24,30),  (38,50),   (76,100),  (114,150)],
    "9:16": [(18,30),  (28,50),   (56,100),  (84,150)],
}
DETAIL_NAMES = ["small", "medium", "large", "extra large"]

def cell_geometry(gw, gh):
    ps = min(CANVAS_W / gw, CANVAS_H / gh)
    ox = (CANVAS_W - ps * gw) / 2
    oy = (CANVAS_H - ps * gh) / 2
    return ps, ox, oy

# ── Palette ── extracted verbatim from color.svg ──────────────────────────────
def _h(h):
    h = h.lstrip("#")
    return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16))

HEARTOPIA_PALETTE = [
    ((1,1),_h("#051616")), ((1,2),_h("#414545")), ((1,3),_h("#808282")),
    ((1,4),_h("#bebfbf")), ((1,5),_h("#feffff")),
    ((2,1),_h("#cf354d")),  ((2,2),_h("#ee6f72")),  ((2,3),_h("#a6263d")),
    ((2,4),_h("#f5aca6")),  ((2,5),_h("#c98483")),  ((2,6),_h("#a35d5e")),
    ((2,7),_h("#682f39")),  ((2,8),_h("#e7d5d5")),  ((2,9),_h("#c0acab")),
    ((2,10),_h("#755e5e")),
    ((3,1),_h("#e95e2b")),  ((3,2),_h("#f98358")),  ((3,3),_h("#ab4226")),
    ((3,4),_h("#feba9f")),  ((3,5),_h("#d9937c")),  ((3,6),_h("#af6c58")),
    ((3,7),_h("#753b31")),  ((3,8),_h("#e9d5d0")),  ((3,9),_h("#c1aca6")),
    ((3,10),_h("#755e59")),
    ((4,1),_h("#f49e16")),  ((4,2),_h("#feae3b")),  ((4,3),_h("#b16f16")),
    ((4,4),_h("#fece92")),  ((4,5),_h("#daa76d")),  ((4,6),_h("#b3814b")),
    ((4,7),_h("#795126")),  ((4,8),_h("#f5e4ce")),  ((4,9),_h("#cdbca9")),
    ((4,10),_h("#806f5e")),
    ((5,1),_h("#edca16")),  ((5,2),_h("#f9d838")),  ((5,3),_h("#b39416")),
    ((5,4),_h("#f9e690")),  ((5,5),_h("#d4be6f")),  ((5,6),_h("#ab954b")),
    ((5,7),_h("#756326")),  ((5,8),_h("#eee7c7")),  ((5,9),_h("#c6bfa2")),
    ((5,10),_h("#787259")),
    ((6,1),_h("#a7bb16")),  ((6,2),_h("#b6c833")),  ((6,3),_h("#758616")),
    ((6,4),_h("#d8df93")),  ((6,5),_h("#acb66c")),  ((6,6),_h("#85914b")),
    ((6,7),_h("#535e2b")),  ((6,8),_h("#e6e9c7")),  ((6,9),_h("#bcc2a3")),
    ((6,10),_h("#6e745d")),
    ((7,1),_h("#05a25d")),  ((7,2),_h("#41b97b")),  ((7,3),_h("#057447")),
    ((7,4),_h("#9cdaad")),  ((7,5),_h("#76b28b")),  ((7,6),_h("#4f8969")),
    ((7,7),_h("#245640")),  ((7,8),_h("#c3e0cc")),  ((7,9),_h("#9db7a6")),
    ((7,10),_h("#53695d")),
    ((8,1),_h("#058781")),  ((8,2),_h("#05aba0")),  ((8,3),_h("#056966")),
    ((8,4),_h("#7ecdc2")),  ((8,5),_h("#55a49c")),  ((8,6),_h("#2b7e78")),
    ((8,7),_h("#054b4b")),  ((8,8),_h("#bee0da")),  ((8,9),_h("#98b7b2")),
    ((8,10),_h("#4e6b66")),
    ((9,1),_h("#05729c")),  ((9,2),_h("#0599ba")),  ((9,3),_h("#055878")),
    ((9,4),_h("#79bbca")),  ((9,5),_h("#5193a5")),  ((9,6),_h("#246d7f")),
    ((9,7),_h("#05495b")),  ((9,8),_h("#c6dde2")),  ((9,9),_h("#9eb5ba")),
    ((9,10),_h("#4f676f")),
    ((10,1),_h("#055ea6")),  ((10,2),_h("#2b83c1")),  ((10,3),_h("#054782")),
    ((10,4),_h("#83a8c9")),  ((10,5),_h("#5d80a1")),  ((10,6),_h("#365b7f")),
    ((10,7),_h("#193b56")),  ((10,8),_h("#c1cdd5")),  ((10,9),_h("#9ba6b0")),
    ((10,10),_h("#4c5967")),
    ((11,1),_h("#534da1")),  ((11,2),_h("#7577bd")),  ((11,3),_h("#3e387e")),
    ((11,4),_h("#a2a0c7")),  ((11,5),_h("#787aa1")),  ((11,6),_h("#55567e")),
    ((11,7),_h("#333555")),  ((11,8),_h("#c9cad5")),  ((11,9),_h("#a2a3b0")),
    ((11,10),_h("#565869")),
    ((12,1),_h("#813d8b")),  ((12,2),_h("#a167a9")),  ((12,3),_h("#602b6c")),
    ((12,4),_h("#b89bb9")),  ((12,5),_h("#907395")),  ((12,6),_h("#6c4d73")),
    ((12,7),_h("#432e4b")),  ((12,8),_h("#cfc9d1")),  ((12,9),_h("#aba1ac")),
    ((12,10),_h("#605664")),
    ((13,1),_h("#ad356f")),  ((13,2),_h("#cf6b8f")),  ((13,3),_h("#862658")),
    ((13,4),_h("#d9a1b4")),  ((13,5),_h("#b3798b")),  ((13,6),_h("#8b5367")),
    ((13,7),_h("#60354b")),  ((13,8),_h("#e4d5da")),  ((13,9),_h("#bcadb1")),
    ((13,10),_h("#725e66")),
]
PALETTE_KEY_TO_RGB = {key: rgb for key, rgb in HEARTOPIA_PALETTE}

# ── Color matching ─────────────────────────────────────────────────────────────
def find_closest_color(r, g, b):
    best_key  = None
    best_dist = float("inf")
    for key, (pr, pg, pb) in HEARTOPIA_PALETTE:
        dist = math.sqrt((r-pr)**2 + (g-pg)**2 + (b-pb)**2)
        if dist < best_dist:
            best_dist = dist
            best_key  = key
    return best_key

# ── Image processing ───────────────────────────────────────────────────────────
def _crop_resize(img, gw, gh):
    nw, nh = img.size
    desired = gw / gh
    if nw / nh > desired:
        sh = nh; sw = round(sh * desired)
        sx = round((nw - sw) / 2); sy = 0
    else:
        sw = nw; sh = round(sw / desired)
        sx = 0;  sy = round((nh - sh) / 2)
    return img.crop((sx, sy, sx+sw, sy+sh)).resize((gw, gh), Image.Resampling.BILINEAR)

def process_image(img_path, ratio, detail_idx, closest_fn=None):
    gw, gh = GRID_DIMENSIONS[ratio][detail_idx]
    img = Image.open(img_path).convert("RGB")
    img = _crop_resize(img, gw, gh)
    if closest_fn is None:
        closest_fn = find_closest_color
    draw_map = {}
    for py in range(gh):
        for px in range(gw):
            r, g, b = img.getpixel((px, py))
            key = closest_fn(r, g, b)
            draw_map.setdefault(key, []).append((px, py))
    return draw_map, gw, gh

# ══════════════════════════════════════════════════════════════════════════════
# CANVAS AUTO-DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _stripe_detect(gray):
    """Two-pass column/row density. Returns (lx, ty, rw, rh) or None."""
    bright = (gray > _STRIPE_THRESHOLD).astype(float)

    # Pass 1: X bounds
    col_density = bright.mean(axis=0)
    col_smooth  = np.convolve(col_density,
                              np.ones(_SMOOTH_WIN) / _SMOOTH_WIN, mode="same")
    high_cols = np.where(col_smooth > _DENSITY_THRESHOLD)[0]
    if len(high_cols) < 10:
        return None
    lx = int(high_cols[0]);  rx = int(high_cols[-1])

    # Pass 2: Y bounds restricted to canvas columns
    row_density = bright[:, lx:rx].mean(axis=1)
    high_rows   = np.where(row_density > _DENSITY_THRESHOLD)[0]
    if len(high_rows) < 10:
        return None
    ty = int(high_rows[0]);  by = int(high_rows[-1])

    rw = rx - lx;  rh = by - ty
    return (lx, ty, rw, rh) if rw > 20 and rh > 20 else None


def _build_cell_to_screen(bx, by, rw, rh, gw, gh):
    cell_w = rw / gw
    cell_h = rh / gh
    xs = [round(bx + x * cell_w + cell_w / 2) for x in range(gw)]
    ys = [round(by + y * cell_h + cell_h / 2) for y in range(gh)]
    dup_x = len(xs) - len(set(xs))
    dup_y = len(ys) - len(set(ys))
    if dup_x or dup_y:
        print(f"  ⚠  {dup_x} dup-X  {dup_y} dup-Y — canvas too small, zoom in.")
    else:
        print(f"  ✓  {gw}×{gh} cells  │  cell {cell_w:.2f}×{cell_h:.2f} px  │  "
              f"X {xs[0]}→{xs[-1]}  Y {ys[0]}→{ys[-1]}")
    def cell_to_screen(x, y):
        return xs[x], ys[y]
    return cell_to_screen


def _detection_preview(bx, by, rw, rh, gw, gh, scale, method, duration=4.0):
    try:
        import tkinter as tk
    except ImportError:
        return
    cell_w = rw / gw;  cell_h = rh / gh
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost",    True)
    root.overrideredirect(True)
    root.attributes("-alpha", 0.82)
    root.configure(bg="black")
    root.wm_attributes("-transparentcolor", "black")
    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill=tk.BOTH, expand=True)
    # Cyan border
    cv.create_rectangle(bx-2, by-2, bx+rw+2, by+rh+2, outline="#00FFFF", width=4)
    # Green grid lines (≤20 per axis)
    for i in range(0, gw+1, max(1, gw//20)):
        x = round(bx + i * cell_w)
        cv.create_line(x, by, x, by+rh, fill="#00DD00", width=1)
    for j in range(0, gh+1, max(1, gh//20)):
        y = round(by + j * cell_h)
        cv.create_line(bx, y, bx+rw, y, fill="#00DD00", width=1)
    # Orange dots at corners + centre
    dot_r = max(4, round(min(cell_w, cell_h) * 0.45))
    for ci, cj in [(0,0),(gw-1,0),(0,gh-1),(gw-1,gh-1),(gw//2,gh//2)]:
        cx_ = round(bx + ci * cell_w + cell_w / 2)
        cy_ = round(by + cj * cell_h + cell_h / 2)
        cv.create_oval(cx_-dot_r, cy_-dot_r, cx_+dot_r, cy_+dot_r,
                       fill="#FF8800", outline="#FFEE00", width=2)
    label = (f"✓  Canvas Detected  [{method.upper()}]\n"
             f"Grid {gw}×{gh}  |  ({bx},{by})  {rw}×{rh}px  |  "
             f"Cell {cell_w:.1f}×{cell_h:.1f}px  |  Scale {scale:.4f}x\n"
             f"Click or wait {int(duration)}s to start painting…")
    ly = max(8, by - 62)
    cv.create_text(bx+2, ly+2, anchor="nw", text=label,
                   fill="#000000", font=("Consolas", 10, "bold"))
    cv.create_text(bx,   ly,   anchor="nw", text=label,
                   fill="#FFFFFF", font=("Consolas", 10, "bold"))
    root.after(int(duration * 1000), root.destroy)
    root.bind("<Button-1>", lambda e: root.destroy())
    root.bind("<Escape>",   lambda e: root.destroy())
    root.mainloop()


def _overlay_drag(gw, gh):
    """Manual drag fallback. Returns (x,y,w,h) or None."""
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError:
        return None
    result = {}
    screenshot = ImageGrab.grab()
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost",    True)
    root.overrideredirect(True)
    root.configure(bg="black")
    cv = tk.Canvas(root, cursor="crosshair", bg="grey15", highlightthickness=0)
    cv.pack(fill=tk.BOTH, expand=True)
    bg    = ImageEnhance.Brightness(screenshot).enhance(0.4)
    bg_tk = ImageTk.PhotoImage(bg)
    cv.create_image(0, 0, anchor="nw", image=bg_tk)
    state   = {"x0": 0, "y0": 0, "down": False}
    rect_id = cv.create_rectangle(0,0,0,0, outline="#00FFFF", width=2)
    info_id = cv.create_text(8, 8, anchor="nw", fill="white",
                              font=("Consolas", 12, "bold"),
                              text=f"Drag over the STRIPED grid  │  {gw}×{gh}  │  ESC=cancel")
    def press(e):
        state.update(x0=e.x, y0=e.y, down=True)
        cv.coords(rect_id, e.x, e.y, e.x, e.y)
    def drag(e):
        if not state["down"]: return
        cv.coords(rect_id, state["x0"], state["y0"], e.x, e.y)
        cv.itemconfig(info_id,
            text=f"({min(state['x0'],e.x)},{min(state['y0'],e.y)})  "
                 f"{abs(e.x-state['x0'])}×{abs(e.y-state['y0'])}px  │  ESC=cancel")
    def release(e):
        state["down"] = False
        x0 = min(state["x0"],e.x);  y0 = min(state["y0"],e.y)
        sw = abs(e.x-state["x0"]);  sh = abs(e.y-state["y0"])
        if sw > 10 and sh > 10:
            result["rect"] = (x0, y0, sw, sh)
        root.destroy()
    cv.bind("<ButtonPress-1>",   press)
    cv.bind("<B1-Motion>",       drag)
    cv.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
    return result.get("rect")


def detect_canvas(gw, gh, show_preview=True):
    """
    Detect the painting grid.  Returns dict with:
      snip_x, snip_y, snip_w, snip_h, scale, method, cell_to_screen
    """
    print(f"\n{'═'*60}")
    print(f"  Canvas Auto-Detection   Grid: {gw}×{gh}")
    print(f"{'═'*60}")

    result = None

    if _HAS_NUMPY:
        print("  Taking screenshot for stripe analysis…")
        try:
            screenshot = ImageGrab.grab()
            arr  = np.array(screenshot)
            gray = arr[:, :, :3].mean(axis=2).astype(float)
            region = _stripe_detect(gray)
            if region:
                lx, ty, rw, rh = region
                ps    = min(CANVAS_W / gw, CANVAS_H / gh)
                scale = ((rw / (ps*gw)) + (rh / (ps*gh))) / 2
                print(f"  Stripe: x={lx}  y={ty}  w={rw}  h={rh}  scale={scale:.4f}x")
                result = dict(snip_x=lx, snip_y=ty, snip_w=rw, snip_h=rh,
                              scale=scale, method="stripe")
            else:
                print("  Stripe detect failed (canvas may be fully painted).")
        except Exception as e:
            print(f"  Stripe detect error: {e}")
    else:
        print("  numpy not installed — skipping auto-detect.")

    if result is None:
        print("  Opening drag overlay — drag over the STRIPED grid area.")
        rect = _overlay_drag(gw, gh)
        if rect is None:
            raise RuntimeError(
                "Canvas detection cancelled.\n"
                "Make sure the game canvas is visible on screen."
            )
        lx, ty, rw, rh = rect
        ps    = min(CANVAS_W / gw, CANVAS_H / gh)
        scale = rw / (ps * gw)
        result = dict(snip_x=lx, snip_y=ty, snip_w=rw, snip_h=rh,
                      scale=scale, method="overlay")

    print("\n  Pre-computing cell coordinates:")
    result["cell_to_screen"] = _build_cell_to_screen(
        result["snip_x"], result["snip_y"],
        result["snip_w"], result["snip_h"], gw, gh)

    print(f"\n  ✓ Ready  [{result['method']}]  "
          f"({result['snip_x']},{result['snip_y']})  "
          f"{result['snip_w']}×{result['snip_h']} px")

    if show_preview:
        print("  Preview — click or wait 4 s to start…")
        _detection_preview(result["snip_x"], result["snip_y"],
                           result["snip_w"], result["snip_h"],
                           gw, gh, result["scale"], result["method"])
    return result

# ══════════════════════════════════════════════════════════════════════════════
# INPUT / CLICK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _pt(x, y):
    return type("P", (), {"x": int(x), "y": int(y)})()

def _get_pos(prompt):
    if prompt: print(prompt)
    input()
    p = pyautogui.position()
    return _pt(p.x, p.y)

def _click(x, y, hold=CLICK_HOLD_DURATION, after=None):
    x, y = int(x), int(y)
    pyautogui.moveTo(x, y); time.sleep(0.01)
    pyautogui.mouseDown(x, y, button="left"); time.sleep(hold)
    pyautogui.mouseUp(x, y, button="left")
    time.sleep(after if after is not None else CANVAS_CLICK_DELAY)

def _paint_cell(cx, cy):
    """
    Double-paint the same centre point twice — NO offset.
    Both clicks land on exactly (cx, cy) so no adjacent cell can bleed.
    Each click fully completes before the next one starts.
    Pause is checked between the two clicks so F10 feels instant.
    """
    _wait_if_paused()
    x, y = int(cx), int(cy)
    pyautogui.moveTo(x, y)
    pyautogui.mouseDown(x, y, button="left")
    time.sleep(PAINT_CLICK_HOLD)
    pyautogui.mouseUp(x, y, button="left")
    time.sleep(PAINT_CLICK_DELAY)

    _wait_if_paused()
    time.sleep(DOUBLE_PAINT_GAP)
    pyautogui.mouseDown(x, y, button="left")
    time.sleep(PAINT_CLICK_HOLD)
    pyautogui.mouseUp(x, y, button="left")
    time.sleep(PAINT_CLICK_DELAY)

def _palette_click(x, y):
    x, y = int(x), int(y)
    pyautogui.moveTo(x, y); time.sleep(PALETTE_MOVE_PAUSE)
    pyautogui.mouseDown(x, y, button="left"); time.sleep(PALETTE_CLICK_HOLD)
    pyautogui.mouseUp(x, y, button="left"); time.sleep(PALETTE_MOVE_PAUSE)

def _drag(fx, fy, tx, ty):
    fx, fy, tx, ty = int(fx), int(fy), int(tx), int(ty)
    pyautogui.moveTo(fx, fy); time.sleep(0.05)
    pyautogui.mouseDown(fx, fy, button="left"); time.sleep(SCROLL_HOLD_BEFORE_DRAG)
    for i in range(1, SCROLL_DRAG_STEPS+1):
        pyautogui.moveTo(int(fx+(tx-fx)*i/SCROLL_DRAG_STEPS),
                         int(fy+(ty-fy)*i/SCROLL_DRAG_STEPS))
        time.sleep(SCROLL_DRAG_DURATION / SCROLL_DRAG_STEPS)
    pyautogui.mouseUp(tx, ty, button="left")

def start_hotkey_listeners():
    try:
        import keyboard
        keyboard.add_hotkey(STOP_HOTKEY,  set_stop,     suppress=False)
        keyboard.add_hotkey(PAUSE_HOTKEY, toggle_pause, suppress=False)
        return True
    except ImportError:
        return False

# ── Ratio / detail selection ───────────────────────────────────────────────────
def select_ratio_and_detail():
    print("Canvas sizes  (ratio → small / medium / large / extra large):")
    ratio_list = list(GRID_DIMENSIONS.keys())
    for ratio in ratio_list:
        row = "  |  ".join(
            f"[{j}] {DETAIL_NAMES[j]} "
            f"{GRID_DIMENSIONS[ratio][j][0]}×{GRID_DIMENSIONS[ratio][j][1]}"
            for j in range(4))
        print(f"  {ratio:5s}  →  {row}")
    print()
    ratio = input(f"Aspect ratio ({', '.join(ratio_list)}) [1:1]: ").strip() or "1:1"
    if ratio not in GRID_DIMENSIONS: ratio = "1:1"

    print(f"\nDetail levels for {ratio}:")
    for j in range(4):
        gw2, gh2 = GRID_DIMENSIONS[ratio][j]
        ps2, ox2, oy2 = cell_geometry(gw2, gh2)
        print(f"  [{j}] {DETAIL_NAMES[j]:12s}  {gw2:3d}×{gh2:3d}  "
              f"({gw2*gh2:6d} cells,  cell={ps2:.2f} canvas px)")

    detail_in = input("Detail (0-3) [1]: ").strip() or "1"
    try:
        detail_idx = int(detail_in)
        if detail_idx not in range(4): detail_idx = 1
    except ValueError:
        detail_idx = {n: i for i, n in enumerate(DETAIL_NAMES)}.get(detail_in.lower(), 1)

    gw, gh = GRID_DIMENSIONS[ratio][detail_idx]
    print(f"→  {ratio}  {DETAIL_NAMES[detail_idx]} : {gw}×{gh}  ({gw*gh} cells)\n")
    return ratio, detail_idx, gw, gh

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=== Heartopia Auto Painter  v6 ===\n")

    default_img = "image.png"
    img_path = (sys.argv[1] if len(sys.argv) > 1
                else input(f"Image path [{default_img}]: ").strip() or default_img)
    if not os.path.isfile(img_path):
        print(f"Error: file not found: {img_path}"); return
    print(f"Image: {img_path}\n")

    ratio, detail_idx, gw, gh = select_ratio_and_detail()

    print("Which in-game palette?")
    ps_in = input("  [s]imple (16 colours in a row)  "
                  "[d]etailed (13 groups) [s]: ").strip().lower() or "s"
    use_detailed = ps_in.startswith("d")

    # ── Auto canvas detection ──────────────────────────────────────────────────
    canvas = detect_canvas(gw, gh, show_preview=True)
    cell_to_screen = canvas["cell_to_screen"]
    print()

    # ── Palette calibration ───────────────────────────────────────────────────
    if use_detailed:
        cal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                CALIBRATION_FILE)
        calibration_loaded = False
        if input("Load saved palette calibration? [Y/n]: ").strip().lower() != "n":
            try:
                import json
                with open(cal_path) as f: data = json.load(f)
                if (data.get("main_center") and
                        len(data.get("sub5_positions",  [])) == 5 and
                        len(data.get("sub10_positions", [])) == 10):
                    main_center     = tuple(data["main_center"])
                    scroll_by_click = data.get("scroll_by_click", True)
                    if scroll_by_click:
                        next_tile_xy = tuple(data["next_tile_xy"])
                        prev_tile_xy = tuple(data["prev_tile_xy"])
                        srs = sre = None
                        srdx = srdy = sldx = sldy = 0
                        sls  = (0, 0)
                    else:
                        next_tile_xy = prev_tile_xy = None
                        srs  = _pt(*data["scroll_right_start"])
                        sre  = _pt(*data["scroll_right_end"])
                        srdx = sre.x-srs.x; srdy = sre.y-srs.y
                        sldx = -srdx;       sldy = -srdy
                        sls  = (sre.x, sre.y)
                    sub5  = [tuple(p) for p in data["sub5_positions"]]
                    sub10 = [tuple(p) for p in data["sub10_positions"]]
                    calibration_loaded = True
                    print(f"Loaded calibration from {cal_path}")
                else:
                    raise ValueError("incomplete data")
            except Exception as e:
                print(f"Could not load ({e}). Doing full calibration.")

        if not calibration_loaded:
            print("\n--- Main palette ---")
            print("Scroll the strip so Group 1 (black) is in the centre slot.")
            p = _get_pos("Hover centre of that black square, ENTER")
            main_center = (p.x, p.y)

            print("\nChange main colour by [c]lick tiles or [d]rag strip? [c]: ", end="")
            scroll_by_click = (input().strip().lower() or "c").startswith("c")
            if scroll_by_click:
                p = _get_pos("Hover NEXT colour tile (right of centre), ENTER")
                next_tile_xy = (p.x, p.y)
                p = _get_pos("Hover PREVIOUS colour tile (left of centre), ENTER")
                prev_tile_xy = (p.x, p.y)
                srs = sre = None
                srdx = srdy = sldx = sldy = 0; sls = (0, 0)
            else:
                srs  = _get_pos("Drag START point, ENTER")
                sre  = _get_pos("Drag END point,   ENTER")
                srdx = sre.x-srs.x; srdy = sre.y-srs.y
                sldx = -srdx;       sldy = -srdy
                sls  = (sre.x, sre.y)
                next_tile_xy = prev_tile_xy = None

            print("\n--- Sub-palette: Group 1 (5 shades) ---")
            sub5 = []
            for i in range(5):
                p = _get_pos(f"  Shade {i+1}/5, ENTER")
                sub5.append((p.x, p.y))

            print("\n--- Sub-palette: Groups 2-13 (10 shades) ---")
            sub10 = []
            for i in range(10):
                p = _get_pos(f"  Shade {i+1}/10, ENTER")
                sub10.append((p.x, p.y))

            try:
                import json
                save = {"main_center":     list(main_center),
                        "scroll_by_click": scroll_by_click,
                        "sub5_positions":  [list(p) for p in sub5],
                        "sub10_positions": [list(p) for p in sub10]}
                if scroll_by_click:
                    save["next_tile_xy"] = list(next_tile_xy)
                    save["prev_tile_xy"] = list(prev_tile_xy)
                else:
                    save["scroll_right_start"] = [srs.x, srs.y]
                    save["scroll_right_end"]   = [sre.x, sre.y]
                with open(cal_path, "w") as f: json.dump(save, f, indent=2)
                print(f"Calibration saved to {cal_path}")
            except Exception as e:
                print(f"Could not save: {e}")

        palette_colors = None

    else:
        p1 = _get_pos("Hover FIRST (leftmost) palette colour, ENTER")
        p2 = _get_pos("Hover LAST  (rightmost) palette colour, ENTER")
        slots = 16
        dx = (p2.x-p1.x)/(slots-1) if slots > 1 else 0
        dy = (p2.y-p1.y)/(slots-1) if slots > 1 else 0
        palette_colors = {}
        for i in range(slots):
            pos = (int(p1.x+dx*i), int(p1.y+dy*i))
            r, gc, b = pyautogui.screenshot().getpixel(pos)
            palette_colors.setdefault((r, gc, b), pos)
        print(f"Palette: {len(palette_colors)} unique colours")

    # ── Process image ──────────────────────────────────────────────────────────
    print("\nProcessing image…")
    if not use_detailed:
        pal_keys = list(palette_colors.keys())
        def _closest_simple(r, gc, b):
            best = pal_keys[0]; bd = float("inf")
            for pr, pg, pb in pal_keys:
                d = math.sqrt((r-pr)**2+(gc-pg)**2+(b-pb)**2)
                if d < bd: bd = d; best = (pr, pg, pb)
            return best
        draw_map, gw, gh = process_image(img_path, ratio, detail_idx,
                                          closest_fn=_closest_simple)
    else:
        draw_map, gw, gh = process_image(img_path, ratio, detail_idx)

    total = sum(len(v) for v in draw_map.values())
    print(f"Unique colours: {len(draw_map)}.  Total cells: {total}.")

    # ── Preview ────────────────────────────────────────────────────────────────
    cell_px = max(1, int(min(1100/gw, 630/gh)))
    preview = Image.new("RGB", (gw*cell_px, gh*cell_px), (255,255,255))
    drw     = ImageDraw.Draw(preview)
    if use_detailed:
        for (group, shade), coords in draw_map.items():
            rgb = PALETTE_KEY_TO_RGB[(group, shade)]
            for px, py in coords:
                x0, y0 = px*cell_px, py*cell_px
                drw.rectangle([x0, y0, x0+cell_px-1, y0+cell_px-1], fill=rgb)
    else:
        for color, coords in draw_map.items():
            for px, py in coords:
                x0, y0 = px*cell_px, py*cell_px
                drw.rectangle([x0, y0, x0+cell_px-1, y0+cell_px-1], fill=color)
    preview_path = os.path.abspath("heartopia_preview.png")
    preview.save(preview_path)
    print(f"Preview: {preview_path}  ({gw*cell_px}×{gh*cell_px} px,  {cell_px} px/cell)")
    try:
        if sys.platform == "win32":    os.startfile(preview_path)
        elif sys.platform == "darwin":
            import subprocess; subprocess.run(["open", preview_path], check=False)
        else:
            import subprocess; subprocess.run(["xdg-open", preview_path], check=False)
    except Exception: pass

    if input("\nProceed with painting? [Y/n]: ").strip().lower() == "n":
        print("Cancelled."); return

    # ── Paint ──────────────────────────────────────────────────────────────────
    global stop_painting, pause_painting
    stop_painting  = False
    pause_painting = False
    _pause_event.set()

    if start_hotkey_listeners():
        print(f"\n>>> {PAUSE_HOTKEY.upper()} = pause/resume   "
              f"{STOP_HOTKEY.upper()} = stop <<<")
    else:
        print("\n(pip install keyboard  for pause/stop hotkeys)")

    print("Painting starts in 5 seconds…")
    time.sleep(5)

    painted_cells = set()

    if use_detailed:
        current_main = 1
        if not scroll_by_click:
            ssp = (srs.x, srs.y)

        for (group, shade) in sorted(draw_map, key=lambda gs: (gs[0], gs[1])):
            if stop_painting: print("Stopped."); break

            while current_main < group:
                if scroll_by_click: _palette_click(*next_tile_xy)
                else: _drag(ssp[0], ssp[1], ssp[0]+srdx, ssp[1]+srdy)
                time.sleep(SCROLL_DELAY_AFTER); current_main += 1

            while current_main > group:
                if scroll_by_click: _palette_click(*prev_tile_xy)
                else: _drag(sls[0], sls[1], sls[0]+sldx, sls[1]+sldy)
                time.sleep(SCROLL_DELAY_AFTER); current_main -= 1

            _palette_click(*main_center)
            time.sleep(DELAY_AFTER_COLOR_SELECT)
            sx2, sy2 = (sub5 if group == 1 else sub10)[shade-1]
            _palette_click(sx2, sy2)
            time.sleep(DELAY_AFTER_COLOR_SELECT)

            for (px, py) in draw_map[(group, shade)]:
                if stop_painting: break
                if (px, py) in painted_cells: continue
                cx, cy = cell_to_screen(px, py)
                _paint_cell(cx, cy)
                painted_cells.add((px, py))
            if stop_painting: break

    else:
        for color in draw_map:
            if stop_painting: break
            _palette_click(*palette_colors[color])
            time.sleep(DELAY_AFTER_COLOR_SELECT)
            for (px, py) in draw_map[color]:
                if stop_painting: break
                if (px, py) in painted_cells: continue
                cx, cy = cell_to_screen(px, py)
                _paint_cell(cx, cy)
                painted_cells.add((px, py))
            if stop_painting: break

    # ── Save state ─────────────────────────────────────────────────────────────
    try:
        import json
        sp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "heartopia_painted.json")
        with open(sp, "w") as f:
            json.dump({"gw": gw, "gh": gh,
                       "painted_count": len(painted_cells),
                       "painted": list(painted_cells)}, f, indent=2)
        print(f"\nPainted {len(painted_cells)} cells.  State → {sp}")
    except Exception as e:
        print(f"\nPainted {len(painted_cells)} cells. (State save failed: {e})")
    print("Done.")


if __name__ == "__main__":
    main()
