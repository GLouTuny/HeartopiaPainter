"""
GLouTuny Painter  v4.2  — 100% Accuracy + Professional UI
════════════════════════════════════════════════════════════════════════════════
Heartopia Auto Painter — full GUI, in-app calibration wizard, bucket-fill engine.

KEY IMPROVEMENTS OVER v3.1
──────────────────────────
  ✓ mss-BASED PIXEL SAMPLING  — 5-10x faster than ImageGrab for verify/streaming.
      Uses mss for all per-pixel reads; bulk canvas grabs still use ImageGrab.
  ✓ ROBUST SHADE SELECTION (zip-style)
      • ALWAYS taps back_button before switching to a new main group.
      • Double-taps each shade button for registration reliability.
      • Full re-select on every verify pass (stale palette state was the #1 source
        of wrong-color cells in v3.1).
  ✓ STREAMING VERIFY — same pattern as zip ``paint.py`` (per-cell queue, not batched)
      • One cell per flush step at ``_cell_center``; double-tap repair like the zip app.
      • ``verify_settle_s`` capped at 0.10s for streaming (matches zip).
      • When streaming is ON, post-pass verify is skipped (same as zip).
  ✓ PER-COLOR POST-PASS VERIFY when streaming is OFF
      • ``_verify_and_repair_color_group`` uses only ``_cell_center`` for sample + repair.
  ✓ REGION BUCKET-FILL with local-base sampling + spill detection (zip-style)
      • Each outline cell's outside neighbor is sampled for the local base RGB.
      • After each interior bucket click, spill-checks outside the component.
      • If spill is detected, region fill is disabled for remaining shades.
  ✓ PAINT TOOL re-assertion before every normal-paint pass.
      • Ensures bucket-tool state can't bleed into pixel painting.
  ✓ ROW_DELAY replaced by interruptible sleep.
  ✓ VERIFY_AUTO_RECOVER: if verification loops without converging, resync UI and skip.
  ✓ All timing matches zip defaults (mouse_down=0.02, after_click=0.06, panel=0.12).
  ✓ GUI unchanged from v3.1 — drop-in replacement.

REQUIREMENTS
────────────
  pip install pyautogui pillow numpy keyboard pynput mss
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pyautogui
from PIL import Image, ImageDraw, ImageEnhance, ImageGrab

try:
    import mss as _mss_module
    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False

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

Point = Tuple[int, int]
RGB   = Tuple[int, int, int]

CANVAS_W = 1100
CANVAS_H = 630
GRID_DIMENSIONS = {
    "16:9": [(30,18),(50,28),(100,56),(150,84)],
    "4:3":  [(30,24),(50,38),(100,76),(150,114)],
    "1:1":  [(30,30),(50,50),(100,100),(150,150)],
    "3:4":  [(24,30),(38,50),(76,100),(114,150)],
    "9:16": [(18,30),(28,50),(56,100),(84,150)],
}
DETAIL_NAMES     = ["Small", "Medium", "Large", "Extra Large"]
CALIBRATION_FILE = "heartopia_calibration.json"

_STRIPE_THR  = 243
_DENSITY_THR = 0.25
_SMOOTH_WIN  = 20

# ── Global stop / pause ────────────────────────────────────────────────────────
stop_painting  = False
pause_painting = False
_pause_event   = threading.Event()
_pause_event.set()

def set_stop():
    global stop_painting
    stop_painting = True

def toggle_pause():
    global pause_painting
    pause_painting = not pause_painting
    if pause_painting: _pause_event.clear()
    else:              _pause_event.set()

def _wait_if_paused(cb=None):
    if pause_painting:
        if cb: cb("⏸  PAUSED — press F10 or Resume")
        _pause_event.wait()

def start_hotkeys():
    try:
        import keyboard
        keyboard.add_hotkey("f12", set_stop,     suppress=False)
        keyboard.add_hotkey("f10", toggle_pause, suppress=False)
        return True
    except ImportError:
        return False

# ── Fast pixel sampling (mss preferred, ImageGrab fallback) ───────────────────
def _get_pixel_fast(x: int, y: int, _sct=None) -> RGB:
    """Fast 1×1 pixel read. Uses mss if available (5-10x faster than ImageGrab).
    Pass a persistent mss instance as _sct to avoid per-call open/close overhead."""
    if _HAS_MSS:
        try:
            sct = _sct if _sct is not None else _mss_module.mss().__enter__()
            mon = {"left": x, "top": y, "width": 1, "height": 1}
            img = sct.grab(mon)
            rgb_bytes = getattr(img, "rgb", None)
            if rgb_bytes and len(rgb_bytes) >= 3:
                return (int(rgb_bytes[0]), int(rgb_bytes[1]), int(rgb_bytes[2]))
            px = img.pixel(0, 0)
            if len(px) == 4: b, g, r, _ = px; return (int(r), int(g), int(b))
            if len(px) == 3: b, g, r = px; return (int(r), int(g), int(b))
        except Exception:
            pass
    try:
        sc = ImageGrab.grab(bbox=(x, y, x+1, y+1))
        return sc.getpixel((0, 0))[:3]
    except Exception:
        return (0, 0, 0)

def _sample_pixels_mss(screen_pts: List[Point]) -> List[Optional[RGB]]:
    """Sample multiple individual pixels using one persistent mss context — fast and accurate.
    Used for verify passes where bulk ImageGrab has coordinate rounding issues on small cells."""
    if not screen_pts: return []
    if not _HAS_MSS:
        return [_get_pixel_fast(x, y) for x, y in screen_pts]
    try:
        with _mss_module.mss() as sct:
            out = []
            for x, y in screen_pts:
                try:
                    mon = {"left": int(x), "top": int(y), "width": 1, "height": 1}
                    img = sct.grab(mon)
                    rgb_bytes = getattr(img, "rgb", None)
                    if rgb_bytes and len(rgb_bytes) >= 3:
                        out.append((int(rgb_bytes[0]), int(rgb_bytes[1]), int(rgb_bytes[2])))
                        continue
                    px = img.pixel(0, 0)
                    if len(px) == 4: b, g, r, _ = px; out.append((int(r), int(g), int(b)))
                    elif len(px) == 3: b, g, r = px; out.append((int(r), int(g), int(b)))
                    else: out.append(None)
                except Exception:
                    out.append(None)
            return out
    except Exception:
        return [_get_pixel_fast(x, y) for x, y in screen_pts]

def _grab_canvas_pixels(canvas_rect: Tuple[int,int,int,int], screen_coords: List[Point]) -> List[Optional[RGB]]:
    """Grab canvas region once and sample multiple screen points."""
    if not screen_coords: return []
    bx, by, bw, bh = canvas_rect
    try:
        img = ImageGrab.grab(bbox=(int(bx), int(by), int(bx+bw), int(by+bh)))
    except Exception:
        return [None] * len(screen_coords)
    out = []
    for cx, cy in screen_coords:
        lx, ly = int(cx-bx), int(cy-by)
        if 0 <= lx < bw and 0 <= ly < bh:
            try: out.append(img.getpixel((lx, ly))[:3])
            except Exception: out.append(None)
        else: out.append(None)
    return out

# ── Palette ────────────────────────────────────────────────────────────────────
def _h(s):
    s = s.lstrip("#")
    return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))

HEARTOPIA_PALETTE = [
    ((1,1),_h("#051616")),((1,2),_h("#414545")),((1,3),_h("#808282")),
    ((1,4),_h("#bebfbf")),((1,5),_h("#feffff")),
    ((2,1),_h("#cf354d")),((2,2),_h("#ee6f72")),((2,3),_h("#a6263d")),
    ((2,4),_h("#f5aca6")),((2,5),_h("#c98483")),((2,6),_h("#a35d5e")),
    ((2,7),_h("#682f39")),((2,8),_h("#e7d5d5")),((2,9),_h("#c0acab")),
    ((2,10),_h("#755e5e")),
    ((3,1),_h("#e95e2b")),((3,2),_h("#f98358")),((3,3),_h("#ab4226")),
    ((3,4),_h("#feba9f")),((3,5),_h("#d9937c")),((3,6),_h("#af6c58")),
    ((3,7),_h("#753b31")),((3,8),_h("#e9d5d0")),((3,9),_h("#c1aca6")),
    ((3,10),_h("#755e59")),
    ((4,1),_h("#f49e16")),((4,2),_h("#feae3b")),((4,3),_h("#b16f16")),
    ((4,4),_h("#fece92")),((4,5),_h("#daa76d")),((4,6),_h("#b3814b")),
    ((4,7),_h("#795126")),((4,8),_h("#f5e4ce")),((4,9),_h("#cdbca9")),
    ((4,10),_h("#806f5e")),
    ((5,1),_h("#edca16")),((5,2),_h("#f9d838")),((5,3),_h("#b39416")),
    ((5,4),_h("#f9e690")),((5,5),_h("#d4be6f")),((5,6),_h("#ab954b")),
    ((5,7),_h("#756326")),((5,8),_h("#eee7c7")),((5,9),_h("#c6bfa2")),
    ((5,10),_h("#787259")),
    ((6,1),_h("#a7bb16")),((6,2),_h("#b6c833")),((6,3),_h("#758616")),
    ((6,4),_h("#d8df93")),((6,5),_h("#acb66c")),((6,6),_h("#85914b")),
    ((6,7),_h("#535e2b")),((6,8),_h("#e6e9c7")),((6,9),_h("#bcc2a3")),
    ((6,10),_h("#6e745d")),
    ((7,1),_h("#05a25d")),((7,2),_h("#41b97b")),((7,3),_h("#057447")),
    ((7,4),_h("#9cdaad")),((7,5),_h("#76b28b")),((7,6),_h("#4f8969")),
    ((7,7),_h("#245640")),((7,8),_h("#c3e0cc")),((7,9),_h("#9db7a6")),
    ((7,10),_h("#53695d")),
    ((8,1),_h("#058781")),((8,2),_h("#05aba0")),((8,3),_h("#056966")),
    ((8,4),_h("#7ecdc2")),((8,5),_h("#55a49c")),((8,6),_h("#2b7e78")),
    ((8,7),_h("#054b4b")),((8,8),_h("#bee0da")),((8,9),_h("#98b7b2")),
    ((8,10),_h("#4e6b66")),
    ((9,1),_h("#05729c")),((9,2),_h("#0599ba")),((9,3),_h("#055878")),
    ((9,4),_h("#79bbca")),((9,5),_h("#5193a5")),((9,6),_h("#246d7f")),
    ((9,7),_h("#05495b")),((9,8),_h("#c6dde2")),((9,9),_h("#9eb5ba")),
    ((9,10),_h("#4f676f")),
    ((10,1),_h("#055ea6")),((10,2),_h("#2b83c1")),((10,3),_h("#054782")),
    ((10,4),_h("#83a8c9")),((10,5),_h("#5d80a1")),((10,6),_h("#365b7f")),
    ((10,7),_h("#193b56")),((10,8),_h("#c1cdd5")),((10,9),_h("#9ba6b0")),
    ((10,10),_h("#4c5967")),
    ((11,1),_h("#534da1")),((11,2),_h("#7577bd")),((11,3),_h("#3e387e")),
    ((11,4),_h("#a2a0c7")),((11,5),_h("#787aa1")),((11,6),_h("#55567e")),
    ((11,7),_h("#333555")),((11,8),_h("#c9cad5")),((11,9),_h("#a2a3b0")),
    ((11,10),_h("#565869")),
    ((12,1),_h("#813d8b")),((12,2),_h("#a167a9")),((12,3),_h("#602b6c")),
    ((12,4),_h("#b89bb9")),((12,5),_h("#907395")),((12,6),_h("#6c4d73")),
    ((12,7),_h("#432e4b")),((12,8),_h("#cfc9d1")),((12,9),_h("#aba1ac")),
    ((12,10),_h("#605664")),
    ((13,1),_h("#ad356f")),((13,2),_h("#cf6b8f")),((13,3),_h("#862658")),
    ((13,4),_h("#d9a1b4")),((13,5),_h("#b3798b")),((13,6),_h("#8b5367")),
    ((13,7),_h("#60354b")),((13,8),_h("#e4d5da")),((13,9),_h("#bcadb1")),
    ((13,10),_h("#725e66")),
]
PALETTE_KEY_TO_RGB: Dict = {k: v for k, v in HEARTOPIA_PALETTE}

def find_closest_color(r, g, b):
    best_key = (1,1); best_d = float("inf")
    for key,(pr,pg,pb) in HEARTOPIA_PALETTE:
        d=(r-pr)**2+(g-pg)**2+(b-pb)**2
        if d<best_d: best_d=d; best_key=key
    return best_key

# ── Image ──────────────────────────────────────────────────────────────────────
def _crop_resize(img, gw, gh):
    nw,nh=img.size; des=gw/gh
    if nw/nh>des: sh=nh;sw=round(sh*des);sx=round((nw-sw)/2);sy=0
    else: sw=nw;sh=round(sw/des);sx=0;sy=round((nh-sh)/2)
    return img.crop((sx,sy,sx+sw,sy+sh)).resize((gw,gh),Image.Resampling.BILINEAR)

def process_image(img_path, gw, gh):
    img=Image.open(img_path).convert("RGB"); img=_crop_resize(img,gw,gh); dm={}
    for py in range(gh):
        for px in range(gw):
            r,g,b=img.getpixel((px,py)); k=find_closest_color(r,g,b)
            dm.setdefault(k,[]).append((px,py))
    return dm

def generate_preview(dm, gw, gh):
    cell=max(1,int(min(1100/gw,630/gh)))
    img=Image.new("RGB",(gw*cell,gh*cell),(30,30,40)); d=ImageDraw.Draw(img)
    for key,coords in dm.items():
        rgb=PALETTE_KEY_TO_RGB.get(key,(128,128,128))
        for px,py in coords:
            x0,y0=px*cell,py*cell; d.rectangle([x0,y0,x0+cell-1,y0+cell-1],fill=rgb)
    return img

# ── Canvas detection ───────────────────────────────────────────────────────────
def _stripe_detect(gray):
    bright=(gray>_STRIPE_THR).astype(float)
    cs=bright.mean(axis=0); cs=np.convolve(cs,np.ones(_SMOOTH_WIN)/_SMOOTH_WIN,mode="same")
    hc=np.where(cs>_DENSITY_THR)[0]
    if len(hc)<10: return None
    lx,rx=int(hc[0]),int(hc[-1])
    rs=bright[:,lx:rx].mean(axis=1); hr=np.where(rs>_DENSITY_THR)[0]
    if len(hr)<10: return None
    ty,by=int(hr[0]),int(hr[-1]); rw,rh=rx-lx,by-ty
    return (lx,ty,rw,rh) if rw>20 and rh>20 else None

def detect_canvas_auto(gw, gh):
    if not _HAS_NUMPY: return None
    try:
        ss=ImageGrab.grab(); arr=np.array(ss)
        gray=arr[:,:,:3].mean(axis=2).astype(float)
        r=_stripe_detect(gray)
        if r:
            lx,ty,rw,rh=r; ps=min(CANVAS_W/gw,CANVAS_H/gh)
            scale=((rw/(ps*gw))+(rh/(ps*gh)))/2
            return dict(snip_x=lx,snip_y=ty,snip_w=rw,snip_h=rh,scale=scale,method="stripe")
    except Exception: pass
    return None

def build_cell_to_screen(canvas, gw, gh):
    """Precompute ``c2s(grid_x, grid_y)`` for speed.

    Uses **exactly** the zip reference formula (``paint._cell_center`` /
    ``_paint_coord_runs``) — no clamping, no “+1 if duplicate” bumps. Those bumps
    shifted taps off the real Heartopia cell grid (vertical gaps, holes, then
    bucket-fill leaking across the canvas).
    """
    x0, y0 = canvas["snip_x"], canvas["snip_y"]
    w, h = canvas["snip_w"], canvas["snip_h"]
    if gw <= 0 or gh <= 0:
        def c2s(_x, _y):
            return int(x0), int(y0)

        canvas["cell_to_screen"] = c2s
        return c2s
    cw = float(w) / float(gw)
    ch = float(h) / float(gh)
    xs = [int(x0 + (x + 0.5) * cw) for x in range(gw)]
    ys = [int(y0 + (y + 0.5) * ch) for y in range(gh)]

    def c2s(x, y):
        return xs[x], ys[y]

    canvas["cell_to_screen"] = c2s
    return c2s

def show_canvas_overlay(canvas, gw, gh, duration=4.0, main_root=None):
    try:
        import tkinter as tk
    except ImportError: return
    bx,by=canvas["snip_x"],canvas["snip_y"]
    rw,rh=canvas["snip_w"],canvas["snip_h"]
    cw,ch=rw/gw,rh/gh; scale=canvas.get("scale",1.0); method=canvas.get("method","?")
    ov=tk.Toplevel(main_root) if main_root else tk.Tk()
    ov.attributes("-fullscreen",True); ov.attributes("-topmost",True)
    ov.overrideredirect(True); ov.attributes("-alpha",0.80)
    ov.configure(bg="black"); ov.wm_attributes("-transparentcolor","black")
    cv=tk.Canvas(ov,bg="black",highlightthickness=0); cv.pack(fill=tk.BOTH,expand=True)
    cv.create_rectangle(bx-2,by-2,bx+rw+2,by+rh+2,outline="#00FFFF",width=4)
    for i in range(0,gw+1,max(1,gw//20)):
        x=round(bx+i*cw); cv.create_line(x,by,x,by+rh,fill="#00DD00",width=1)
    for j in range(0,gh+1,max(1,gh//20)):
        y=round(by+j*ch); cv.create_line(bx,y,bx+rw,y,fill="#00DD00",width=1)
    dr=max(4,round(min(cw,ch)*0.45))
    for ci,cj in [(0,0),(gw-1,0),(0,gh-1),(gw-1,gh-1),(gw//2,gh//2)]:
        cx_=round(bx+ci*cw+cw/2); cy_=round(by+cj*ch+ch/2)
        cv.create_oval(cx_-dr,cy_-dr,cx_+dr,cy_+dr,fill="#FF8800",outline="#FFEE00",width=2)
    txt=(f"✓ Canvas Detected [{method.upper()}]  Grid {gw}×{gh}  "
         f"({bx},{by})  {rw}×{rh}px  Cell {cw:.1f}×{ch:.1f}px  Scale {scale:.4f}x\n"
         f"Click or wait {int(duration)}s to dismiss…")
    ly=max(8,by-50)
    cv.create_text(bx+2,ly+2,anchor="nw",text=txt,fill="#000000",font=("Consolas",10,"bold"))
    cv.create_text(bx,ly,    anchor="nw",text=txt,fill="#FFFFFF", font=("Consolas",10,"bold"))
    ov.after(int(duration*1000),ov.destroy)
    ov.bind("<Button-1>",lambda e:ov.destroy()); ov.bind("<Escape>",lambda e:ov.destroy())
    if main_root: ov.wait_window(ov)
    else: ov.mainloop()

def overlay_drag_select(gw, gh, main_root=None):
    try:
        import tkinter as tk
        from PIL import ImageTk
    except ImportError: return None
    result={}; ss=ImageGrab.grab()
    ov=tk.Toplevel(main_root) if main_root else tk.Tk()
    ov.attributes("-fullscreen",True); ov.attributes("-topmost",True)
    ov.overrideredirect(True)
    cv=tk.Canvas(ov,cursor="crosshair",bg="grey15",highlightthickness=0)
    cv.pack(fill=tk.BOTH,expand=True)
    try:
        bg=ImageEnhance.Brightness(ss).enhance(0.72)
        bgi=ImageTk.PhotoImage(bg); cv.create_image(0,0,anchor="nw",image=bgi)
    except Exception: pass
    st={"x0":0,"y0":0,"dn":False}
    rid=cv.create_rectangle(0,0,0,0,outline="#00FFFF",width=2)
    iid=cv.create_text(8,8,anchor="nw",fill="white",font=("Consolas",12,"bold"),
                        text=f"Drag over STRIPED grid area  |  {gw}×{gh}  |  ESC=cancel")
    def press(e): st.update(x0=e.x,y0=e.y,dn=True); cv.coords(rid,e.x,e.y,e.x,e.y)
    def drag(e):
        if not st["dn"]: return
        cv.coords(rid,st["x0"],st["y0"],e.x,e.y)
        cv.itemconfig(iid,text=f"({min(st['x0'],e.x)},{min(st['y0'],e.y)})  "
                      f"{abs(e.x-st['x0'])}×{abs(e.y-st['y0'])}px  |  ESC=cancel")
    def release(e):
        st["dn"]=False; x0=min(st["x0"],e.x); y0=min(st["y0"],e.y)
        sw=abs(e.x-st["x0"]); sh=abs(e.y-st["y0"])
        if sw>10 and sh>10: result["r"]=(x0,y0,sw,sh)
        ov.destroy()
    cv.bind("<ButtonPress-1>",press); cv.bind("<B1-Motion>",drag)
    cv.bind("<ButtonRelease-1>",release); ov.bind("<Escape>",lambda e:ov.destroy())
    if main_root: ov.wait_window(ov)
    else: ov.mainloop()
    return result.get("r")

# ── Click helpers ──────────────────────────────────────────────────────────────
@dataclass
class PaintOptions:
    move_dur:           float = 0.03
    hold_s:             float = 0.02    # mouse_down_s
    after_s:            float = 0.06    # after_click_delay_s
    pal_move_s:         float = 0.08
    pal_hold_s:         float = 0.12    # panel_open_delay_s
    pal_after_s:        float = 0.08
    color_settle:       float = 0.06    # shade_select_delay_s
    drag_step_s:        float = 0.01
    after_drag_s:       float = 0.02
    row_delay_s:        float = 0.0
    enable_drag:        bool  = True
    rapid_click_strokes:bool  = True    # click-per-cell in run (zip-style default)
    double_paint:       bool  = False   # extra tap per cell (slower but reliable fallback)
    bucket_min:         int   = 50      # min cells for base bucket fill
    bucket_regions:     bool  = True
    region_min:         int   = 200
    verify_tol:         int   = 35
    verify_max_passes:  int   = 10
    verify_settle_s:    float = 0.05
    verify_streaming:   bool  = True
    verify_stream_lag:  int   = 10
    verify_auto_recover:bool  = True
    verify_recover_after:int  = 2

def _tap(x, y, opts: PaintOptions, extra_delay: float = 0.0):
    x, y = int(x), int(y)
    pyautogui.moveTo(x, y, duration=opts.move_dur)
    pyautogui.mouseDown(button="left")
    time.sleep(opts.hold_s)
    pyautogui.mouseUp(button="left")
    time.sleep(opts.after_s + extra_delay)

def _pal_tap(x, y, opts: PaintOptions, extra: float = 0.0):
    x, y = int(x), int(y)
    pyautogui.moveTo(x, y, duration=opts.pal_move_s)
    pyautogui.mouseDown(button="left")
    time.sleep(opts.pal_hold_s)
    pyautogui.mouseUp(button="left")
    time.sleep(opts.pal_after_s + extra)

def _sleep_with_stop(duration: float, should_stop: Optional[Callable[[], bool]] = None) -> bool:
    """Sleep interruptibly. Returns False if stopped early."""
    d = max(0.0, float(duration))
    if d <= 0: return True
    end = time.perf_counter() + d
    while True:
        if should_stop and should_stop(): return False
        now = time.perf_counter()
        if now >= end: return True
        time.sleep(min(0.02, max(0.0, end - now)))

def _stroke_pynput(pts, opts: PaintOptions, should_stop=None):
    try:
        from pynput.mouse import Button, Controller
        mouse = Controller(); mouse.position = pts[0]; mouse.press(Button.left)
        time.sleep(opts.hold_s); n = 6; step = max(0.0, opts.drag_step_s)
        for t in pts[1:]:
            if should_stop and should_stop(): break
            x0, y0 = mouse.position; x1, y1 = t; dx, dy = x1-x0, y1-y0
            for i in range(1, n+1):
                if should_stop and should_stop(): break
                mouse.position = (int(round(x0+dx*i/n)), int(round(y0+dy*i/n)))
                if step > 0: time.sleep(step/n)
        mouse.release(Button.left); time.sleep(opts.after_drag_s); return True
    except Exception: return False

def _stroke_pyautogui(pts, opts: PaintOptions, should_stop=None):
    pyautogui.moveTo(pts[0][0], pts[0][1], duration=opts.move_dur)
    pyautogui.mouseDown(button="left"); time.sleep(opts.hold_s)
    try:
        cx, cy = pts[0]; n = 6; step = max(0.0, opts.drag_step_s)
        for px, py in pts[1:]:
            if should_stop and should_stop(): return
            dx, dy = px-cx, py-cy
            for i in range(1, n+1):
                if should_stop and should_stop(): return
                pyautogui.moveTo(int(round(cx+dx*i/n)), int(round(cy+dy*i/n)), duration=0)
                if step > 0: time.sleep(step/n)
            cx, cy = px, py
    finally: pyautogui.mouseUp(button="left")
    time.sleep(opts.after_drag_s)

def _rapid_click_stroke(pts, opts: PaintOptions, should_stop=None):
    """Click every point in a run — zip-style reliable stroke."""
    step = max(0.0, opts.drag_step_s)
    after = max(0.0, opts.after_drag_s)
    for px, py in pts:
        if should_stop and should_stop(): return
        pyautogui.moveTo(int(px), int(py), duration=0)
        pyautogui.mouseDown(button="left")
        if opts.hold_s > 0: time.sleep(opts.hold_s)
        pyautogui.mouseUp(button="left")
        if step > 0: time.sleep(step)
    if after > 0: time.sleep(after)

def _paint_run(pts, opts: PaintOptions, should_stop=None):
    if not pts: return
    if opts.enable_drag and len(pts) >= 2:
        if opts.rapid_click_strokes:
            _rapid_click_stroke(pts, opts, should_stop)
        else:
            if not _stroke_pynput(pts, opts, should_stop):
                _stroke_pyautogui(pts, opts, should_stop)
    else:
        for p in pts:
            if should_stop and should_stop(): return
            _tap(p[0], p[1], opts)
            if opts.double_paint:
                time.sleep(0.01); _tap(p[0], p[1], opts)

def _cell_center(canvas_rect, gw, gh, x, y):
    """Same formula as Heartopia-Image-Painter ``_cell_center`` / ``build_cell_to_screen``."""
    x0, y0, w, h = canvas_rect
    cw = float(w) / float(gw)
    ch = float(h) / float(gh)
    return (int(x0 + (x + 0.5) * cw), int(y0 + (y + 0.5) * ch))

def _d2(a, b): return (a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2

# ── Calibration data ───────────────────────────────────────────────────────────
def _rgb_to_hex(rgb):
    if not rgb or len(rgb)<3: return ""
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0])&255, int(rgb[1])&255, int(rgb[2])&255)

@dataclass
class ShadeButton:
    name: str = ""
    pos: Optional[Point] = None
    rgb: Optional[RGB] = None

@dataclass
class MainColor:
    name: str = ""
    pos: Optional[Point] = None
    rgb: Optional[RGB] = None
    shades: List[ShadeButton] = field(default_factory=list)

def _default_main_colors():
    out = []
    for g in range(1, 14):
        n_shades = 5 if g == 1 else 10
        shades = [ShadeButton(name=f"Shade {i+1}", pos=None, rgb=None) for i in range(n_shades)]
        out.append(MainColor(name=f"Group {g}", pos=None, rgb=None, shades=shades))
    return out

@dataclass
class CalibData:
    main_center:          Optional[Point] = None
    scroll_by_click:      bool            = True
    next_tile_xy:         Optional[Point] = None
    prev_tile_xy:         Optional[Point] = None
    scroll_rs:            Optional[Point] = None
    scroll_re:            Optional[Point] = None
    sub5:                 List[Point]     = field(default_factory=list)
    sub10:                List[Point]     = field(default_factory=list)
    paint_tool_pos:       Optional[Point] = None
    bucket_tool_pos:      Optional[Point] = None
    shade_panel_open_xy:  Optional[Point] = None
    shade_panel_back_xy:  Optional[Point] = None
    main_center_rgb:      Optional[RGB]   = None
    sub5_rgbs:            List[Optional[RGB]] = field(default_factory=lambda:[None]*5)
    sub10_rgbs:           List[Optional[RGB]] = field(default_factory=lambda:[None]*10)
    main_colors:          List[MainColor]  = field(default_factory=_default_main_colors)

    @property
    def is_complete(self):
        if self.main_colors and len(self.main_colors) >= 13 and self.shade_panel_back_xy:
            for i, mc in enumerate(self.main_colors[:13]):
                need = 5 if i == 0 else 10
                if not mc.pos or len(mc.shades) < need: break
                if any(not s.pos for s in mc.shades[:need]): break
            else:
                return True
        return (self.main_center is not None
                and len(self.sub5) == 5
                and len(self.sub10) == 10)

    def to_dict(self):
        lp=lambda p:list(p) if p else None
        d={"main_center":lp(self.main_center),"scroll_by_click":self.scroll_by_click,
           "sub5_positions":[list(p) for p in self.sub5],
           "sub10_positions":[list(p) for p in self.sub10]}
        if self.scroll_by_click:
            d["next_tile_xy"]=lp(self.next_tile_xy); d["prev_tile_xy"]=lp(self.prev_tile_xy)
        else:
            d["scroll_right_start"]=lp(self.scroll_rs); d["scroll_right_end"]=lp(self.scroll_re)
        if self.paint_tool_pos:  d["paint_tool_pos"] =list(self.paint_tool_pos)
        if self.bucket_tool_pos: d["bucket_tool_pos"]=list(self.bucket_tool_pos)
        if self.shade_panel_open_xy: d["shade_panel_open_xy"]=list(self.shade_panel_open_xy)
        if self.shade_panel_back_xy: d["shade_panel_back_xy"]=list(self.shade_panel_back_xy)
        if self.main_center_rgb: d["main_center_rgb"]=list(self.main_center_rgb)
        if any(self.sub5_rgbs): d["sub5_rgbs"]=[list(r) if r else None for r in self.sub5_rgbs]
        if any(self.sub10_rgbs): d["sub10_rgbs"]=[list(r) if r else None for r in self.sub10_rgbs]
        if self.main_colors:
            def mc_to_dict(mc):
                return {"name":mc.name,"pos":list(mc.pos) if mc.pos else None,"rgb":list(mc.rgb) if mc.rgb else None,
                        "shades":[{"name":s.name,"pos":list(s.pos) if s.pos else None,"rgb":list(s.rgb) if s.rgb else None} for s in mc.shades]}
            d["main_colors"]=[mc_to_dict(m) for m in self.main_colors]
        return d

    def to_zip_config_dict(self):
        def lp(p): return list(p) if p else [0,0]
        def lr(rgb): return list(rgb) if rgb and len(rgb)>=3 else [0,0,0]
        if self.main_colors and len(self.main_colors) >= 13:
            main_colors = []
            for mc in self.main_colors[:13]:
                main_colors.append({"name":mc.name,"pos":lp(mc.pos),"rgb":lr(mc.rgb),
                    "shades":[{"name":s.name,"pos":lp(s.pos),"rgb":lr(s.rgb)} for s in mc.shades]})
        else:
            main_pos=lp(self.main_center); main_rgb=lr(self.main_center_rgb)
            shades5=[{"name":f"Shade {i+1}","pos":lp(self.sub5[i]) if i<len(self.sub5) and self.sub5[i] else [0,0],"rgb":lr(self.sub5_rgbs[i]) if i<len(self.sub5_rgbs) and self.sub5_rgbs[i] else [0,0,0]} for i in range(5)]
            shades10=[{"name":f"Shade {i+1}","pos":lp(self.sub10[i]) if i<len(self.sub10) and self.sub10[i] else [0,0],"rgb":lr(self.sub10_rgbs[i]) if i<len(self.sub10_rgbs) and self.sub10_rgbs[i] else [0,0,0]} for i in range(10)]
            main_colors=[{"name":"Group 1","pos":main_pos,"rgb":main_rgb,"shades":shades5}]
            for g in range(2,14): main_colors.append({"name":f"Group {g}","pos":main_pos,"rgb":main_rgb,"shades":shades10})
        return {"shades_panel_button_pos":lp(self.shade_panel_open_xy or self.main_center),
                "back_button_pos":lp(self.shade_panel_back_xy),
                "paint_tool_button_pos":lp(self.paint_tool_pos),
                "bucket_tool_button_pos":lp(self.bucket_tool_pos),
                "main_colors":main_colors}

    @staticmethod
    def from_dict(d):
        p2=lambda v:tuple(int(x) for x in v) if v else None
        r2=lambda v:tuple(int(x) for x in v) if v and len(v)>=3 else None
        c=CalibData()
        c.main_center    =p2(d.get("main_center"))
        c.scroll_by_click=bool(d.get("scroll_by_click",True))
        c.next_tile_xy   =p2(d.get("next_tile_xy"))
        c.prev_tile_xy   =p2(d.get("prev_tile_xy"))
        c.scroll_rs      =p2(d.get("scroll_right_start"))
        c.scroll_re      =p2(d.get("scroll_right_end"))
        c.sub5           =[p2(x) for x in d.get("sub5_positions",[])]
        c.sub10          =[p2(x) for x in d.get("sub10_positions",[])]
        c.paint_tool_pos      =p2(d.get("paint_tool_pos"))
        c.bucket_tool_pos     =p2(d.get("bucket_tool_pos"))
        c.shade_panel_open_xy =p2(d.get("shade_panel_open_xy"))
        c.shade_panel_back_xy =p2(d.get("shade_panel_back_xy"))
        c.main_center_rgb     =r2(d.get("main_center_rgb"))
        c.sub5_rgbs           =[r2(x) for x in d.get("sub5_rgbs",[None]*5)][:5]
        while len(c.sub5_rgbs)<5: c.sub5_rgbs.append(None)
        c.sub10_rgbs          =[r2(x) for x in d.get("sub10_rgbs",[None]*10)][:10]
        while len(c.sub10_rgbs)<10: c.sub10_rgbs.append(None)
        raw_mains=d.get("main_colors")
        if isinstance(raw_mains,list) and raw_mains:
            c.main_colors=[]
            for mc in raw_mains:
                if not isinstance(mc,dict): continue
                shades=[]
                for sh in mc.get("shades",[]):
                    if isinstance(sh,dict): shades.append(ShadeButton(name=str(sh.get("name","Shade")),pos=p2(sh.get("pos")),rgb=r2(sh.get("rgb"))))
                c.main_colors.append(MainColor(name=str(mc.get("name","Group")),pos=p2(mc.get("pos")),rgb=r2(mc.get("rgb")),shades=shades))
            default_slots=_default_main_colors()
            while len(c.main_colors)<13: c.main_colors.append(default_slots[len(c.main_colors)])
            c.main_colors=c.main_colors[:13]
        return c

    @staticmethod
    def from_zip_config_dict(d):
        p2=lambda v:tuple(int(x) for x in v) if v and len(v)>=2 else None
        r2=lambda v:tuple(int(x) for x in v) if v and len(v)>=3 else None
        c=CalibData()
        mains=d.get("main_colors",[])
        c.shade_panel_open_xy =p2(d.get("shades_panel_button_pos"))
        c.shade_panel_back_xy =p2(d.get("back_button_pos"))
        c.paint_tool_pos      =p2(d.get("paint_tool_button_pos"))
        c.bucket_tool_pos     =p2(d.get("bucket_tool_button_pos"))
        if mains:
            c.main_colors=[]
            for mc in mains:
                if not isinstance(mc,dict): continue
                shades=[]
                for sh in mc.get("shades",[]):
                    if isinstance(sh,dict): shades.append(ShadeButton(name=str(sh.get("name","Shade")),pos=p2(sh.get("pos")),rgb=r2(sh.get("rgb"))))
                c.main_colors.append(MainColor(name=str(mc.get("name","Group")),pos=p2(mc.get("pos")),rgb=r2(mc.get("rgb")),shades=shades))
            default_slots=_default_main_colors()
            while len(c.main_colors)<13: c.main_colors.append(default_slots[len(c.main_colors)])
            c.main_colors=c.main_colors[:13]
            if c.main_colors and c.main_colors[0].pos:
                c.main_center=c.main_colors[0].pos; c.main_center_rgb=c.main_colors[0].rgb
            if c.main_colors and len(c.main_colors[0].shades)==5:
                c.sub5=[s.pos for s in c.main_colors[0].shades if s.pos]
                c.sub5_rgbs=[s.rgb for s in c.main_colors[0].shades]
                while len(c.sub5)<5: c.sub5.append(None)
                while len(c.sub5_rgbs)<5: c.sub5_rgbs.append(None)
            if len(c.main_colors)>=2 and len(c.main_colors[1].shades)==10:
                c.sub10=[s.pos for s in c.main_colors[1].shades if s.pos]
                c.sub10_rgbs=[s.rgb for s in c.main_colors[1].shades]
                while len(c.sub10)<10: c.sub10.append(None)
                while len(c.sub10_rgbs)<10: c.sub10_rgbs.append(None)
        return c

# ── Palette controller (zip-style: ALWAYS back before switching main) ──────────
class PaletteCtrl:
    def __init__(self, cal: CalibData, opts: PaintOptions):
        self.cal = cal; self.opts = opts
        self._last_main: Optional[int] = None  # 1-based group index
        self._in_panel = False

    def _back(self):
        if self.cal.shade_panel_back_xy:
            _pal_tap(*self.cal.shade_panel_back_xy, self.opts)
            time.sleep(self.opts.color_settle)
        self._in_panel = False

    def select(self, group: int, shade: int, cb=None):
        """Select group+shade. zip-style: ALWAYS back, select main, open panel, select shade, back."""
        cal = self.cal; opts = self.opts

        # ── Per-group (zip-style) ─────────────────────────────────────────────
        if (cal.main_colors and len(cal.main_colors) >= group
                and cal.shade_panel_back_xy
                and 1 <= shade <= len(cal.main_colors[group-1].shades)):
            mc = cal.main_colors[group-1]
            sh = mc.shades[shade-1]
            if mc.pos and sh.pos:
                # ALWAYS tap back first (zip ensures consistent UI state)
                self._back()
                # Click the main color tile
                _pal_tap(*mc.pos, opts)
                time.sleep(opts.color_settle)
                # Open the shades panel
                open_xy = cal.shade_panel_open_xy or mc.pos
                _pal_tap(*open_xy, opts, extra=opts.pal_hold_s)
                time.sleep(opts.color_settle)
                # Select the shade — double-tap for reliability (zip does this)
                _pal_tap(*sh.pos, opts, extra=opts.color_settle)
                _pal_tap(*sh.pos, opts)
                time.sleep(0.02)
                # Close the panel
                self._back()
                self._last_main = group
                return

        # ── Legacy fallback ───────────────────────────────────────────────────
        self._back()
        open_xy = cal.shade_panel_open_xy or cal.main_center
        if open_xy:
            _pal_tap(*open_xy, opts, extra=opts.pal_hold_s)
            time.sleep(opts.color_settle)
        subs = cal.sub5 if group == 1 else cal.sub10
        if shade <= len(subs) and subs[shade-1]:
            _pal_tap(*subs[shade-1], opts, extra=opts.color_settle)
            _pal_tap(*subs[shade-1], opts)
            time.sleep(0.02)
        self._back()
        self._last_main = group

# ── Connected component helpers ────────────────────────────────────────────────
def _connected_components(coords):
    cs = set(map(tuple, coords)); vis = set(); out = []
    for start in cs:
        if start in vis: continue
        comp = []; stack = [start]
        while stack:
            p = stack.pop()
            if p in vis: continue
            vis.add(p); comp.append(p); x, y = p
            for nb in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if nb in cs and nb not in vis: stack.append(nb)
        out.append(comp)
    return out

def _boundary_and_interior(comp):
    cs = set(map(tuple, comp)); bnd = []; interior = None
    for px, py in comp:
        if any((px+dx,py+dy) not in cs for dx,dy in ((-1,0),(1,0),(0,-1),(0,1))):
            bnd.append((px,py))
        elif interior is None:
            interior = (px, py)
    return bnd, interior

def _interior_components(comp, boundary):
    comp_set = set(map(tuple, comp)); bnd_set = set(map(tuple, boundary))
    interior_set = comp_set - bnd_set; out = []
    while interior_set:
        start = next(iter(interior_set)); stack = [start]; sub = []
        interior_set.discard(start)
        while stack:
            p = stack.pop(); sub.append(p); x, y = p
            for nb in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                if nb in interior_set: interior_set.discard(nb); stack.append(nb)
        out.append(sub)
    return out

# ── Paint coord runs ───────────────────────────────────────────────────────────
def _paint_runs(
    coords,
    c2s,
    opts,
    should_stop=None,
    progress_cb=None,
    canvas_rect=None,
    gw: Optional[int] = None,
    gh: Optional[int] = None,
):
    """Paint horizontal runs — screen points match zip ``_paint_coord_runs`` when
    ``canvas_rect``/``gw``/``gh`` are passed (same ``int(x0+(rx+0.5)*cell_w)``)."""
    coords = sorted(coords, key=lambda xy: (xy[1], xy[0]))
    i = 0
    while i < len(coords):
        if should_stop and should_stop():
            return
        x, y = coords[i]
        run = [(x, y)]
        j = i + 1
        while j < len(coords):
            nx, ny = coords[j]
            if ny != y or nx != run[-1][0] + 1:
                break
            run.append((nx, ny))
            j += 1
        if canvas_rect is not None and gw is not None and gh is not None:
            pts = [_cell_center(canvas_rect, gw, gh, rx, ry) for rx, ry in run]
        else:
            pts = [c2s(rx, ry) for rx, ry in run]
        _paint_run(pts, opts, should_stop)
        if progress_cb:
            for rx, ry in run:
                progress_cb(rx, ry)
        i = j

def _sample_base_rgb(canvas_rect, c2s, gw, gh):
    pts = [(gw//2,gh//2),(gw//4,gh//4),(3*gw//4,gh//4),(gw//4,3*gh//4),(3*gw//4,3*gh//4)]
    screen_pts = [c2s(x,y) for x,y in pts]
    rgbs = [r for r in _grab_canvas_pixels(canvas_rect, screen_pts) if r is not None]
    if not rgbs: return None
    rs=sorted(r[0] for r in rgbs); gs=sorted(r[1] for r in rgbs); bs=sorted(r[2] for r in rgbs)
    m = len(rgbs)//2
    return (rs[m], gs[m], bs[m])

# ── Verify and repair (zip-style per-color-group, full re-select each pass) ───
def _verify_and_repair_color_group(
    coords,
    canvas_rect,
    gw: int,
    gh: int,
    shade_rgb,
    group,
    shade,
    pal,
    opts,
    should_stop=None,
    status_cb=None,
    progress_cb=None,
):
    """Per-shade verify/repair — aligned with zip ``_verify_and_repair_color_group``.

    Samples and taps use ``_cell_center(canvas_rect, gw, gh, x, y)`` only (same as
    ``get_screen_pixel_rgb`` positions in the reference).
    """
    if not coords:
        return
    tol2 = opts.verify_tol ** 2
    settle = max(0.0, opts.verify_settle_s)
    max_p = opts.verify_max_passes
    coords_sorted = sorted(coords, key=lambda xy: (xy[1], xy[0]))

    for _pass in range(max_p):
        if should_stop and should_stop():
            return
        if settle > 0:
            if not _sleep_with_stop(settle, should_stop):
                return
        if status_cb:
            status_cb(f"  Verify pass {_pass+1}/{max_p}: checking {len(coords_sorted)} cells…")

        screen_pts = [_cell_center(canvas_rect, gw, gh, px, py) for px, py in coords_sorted]
        rgbs = _sample_pixels_mss(screen_pts)
        misses = [
            (px, py)
            for (px, py), rgb in zip(coords_sorted, rgbs)
            if rgb is not None and _d2(rgb, shade_rgb) > tol2
        ]
        if not misses:
            if status_cb:
                status_cb("  ✓ Verified!")
            return

        miss_n = len(misses)
        if status_cb:
            status_cb(f"  Repairing {miss_n} misses (pass {_pass+1})…")

        pal.select(group, shade, status_cb)

        misses.sort(key=lambda xy: (xy[1], xy[0]))
        i = 0
        while i < len(misses):
            if should_stop and should_stop():
                return
            x, y = misses[i]
            run = [(x, y)]
            j = i + 1
            while j < len(misses):
                nx, ny = misses[j]
                if ny != y or nx != run[-1][0] + 1:
                    break
                run.append((nx, ny))
                j += 1
            pts = [_cell_center(canvas_rect, gw, gh, rx, ry) for rx, ry in run]
            for cx, cy in pts:
                    if should_stop and should_stop():
                        return
                    _tap(cx, cy, opts)
            if progress_cb:
                for rx, ry in run:
                    progress_cb(rx, ry)
            i = j

    if status_cb:
        status_cb(f"  ⚠ Verify did not converge after {max_p} passes; continuing…")

def _verify_outline(
    outline_coords, c2s, canvas_rect, shade_rgb, base_rgb,
    opts, pal, group, shade, should_stop=None, status_cb=None,
    local_base_fn=None
):
    """Verify outline pixels. Returns True if outline is clean enough for bucket fill."""
    if not outline_coords: return True
    tol2 = opts.verify_tol ** 2
    base_tol2 = max(8, opts.verify_tol // 2) ** 2
    settle = max(0.0, opts.verify_settle_s)
    max_p = min(5, opts.verify_max_passes)

    coords = list(outline_coords)
    for _pass in range(max_p):
        if should_stop and should_stop(): return False
        if settle > 0:
            if not _sleep_with_stop(settle, should_stop): return False

        screen_pts = [c2s(px,py) for px,py in coords]
        rgbs = _sample_pixels_mss(screen_pts)
        misses = []
        for (px,py), rgb in zip(coords, rgbs):
            if rgb is None: continue
            local_base = base_rgb
            if local_base_fn:
                try: local_base = local_base_fn(px, py) or base_rgb
                except Exception: pass
            if base_rgb is not None:
                looks_base = _d2(rgb, base_rgb) <= base_tol2
                if local_base and local_base != base_rgb:
                    looks_base = looks_base or (_d2(rgb, local_base) <= base_tol2)
                looks_expected = _d2(rgb, shade_rgb) <= tol2
                if looks_base and not looks_expected: misses.append((px,py))
            else:
                if _d2(rgb, shade_rgb) > tol2: misses.append((px,py))

        if not misses: return True
        if status_cb: status_cb(f"  Outline verify: repairing {len(misses)} holes…")

        if pal.cal.paint_tool_pos: _tap(*pal.cal.paint_tool_pos, opts)
        for px, py in misses:
            if should_stop and should_stop(): return False
            cx, cy = c2s(px, py)
            _tap(cx, cy, opts)
            _tap(cx, cy, opts, extra_delay=0.01)

    return False  # didn't converge within max passes

# ── Main paint engine ──────────────────────────────────────────────────────────
def paint_by_color(
    draw_map, gw, gh, c2s, pal: PaletteCtrl, canvas_rect, opts: PaintOptions,
    progress_cb=None, status_cb=None, should_stop=None
):
    global stop_painting
    ordered = sorted(draw_map.items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for v in draw_map.values())
    done = 0
    pp = pal.cal.paint_tool_pos; bp = pal.cal.bucket_tool_pos
    has_bucket = bool(pp and bp)
    tol2 = opts.verify_tol ** 2
    base_tol2 = max(8, opts.verify_tol // 2) ** 2

    def _done(coords):
        nonlocal done
        done += len(coords)
        if progress_cb:
            for px, py in coords: progress_cb(px, py, done, total)

    # ── Base bucket-fill (most-used shade) ─────────────────────────────────────
    base_rgb: Optional[RGB] = None
    bucket_key = None
    if has_bucket and opts.bucket_min > 0 and ordered:
        mk, mc = ordered[0]
        if len(mc) >= opts.bucket_min:
            g, s = mk
            if status_cb: status_cb(f"🪣  Base fill: group {g}, shade {s} ({len(mc)} cells)…")
            pal.select(g, s, status_cb)
            _tap(*bp, opts)
            bx, by, bw, bh = canvas_rect
            _tap(int(bx+bw*0.5), int(by+bh*0.5), opts)
            time.sleep(max(0.12, opts.verify_settle_s))
            if pp: _tap(*pp, opts)
            base_rgb = _sample_base_rgb(canvas_rect, c2s, gw, gh)
            if base_rgb is None: base_rgb = PALETTE_KEY_TO_RGB.get(mk, (128,128,128))
            bucket_key = mk
            _done(mc)
            ordered = [(k,v) for k,v in ordered if k != mk]

    bucket_regions_ok = has_bucket and opts.bucket_regions and bucket_key is not None
    disable_region_fill = False

    for (group, shade), coords in ordered:
        if stop_painting or (should_stop and should_stop()): break
        _wait_if_paused(status_cb)
        shade_rgb = PALETTE_KEY_TO_RGB.get((group, shade), (128,128,128))
        if status_cb: status_cb(f"🎨  Group {group}, Shade {shade}  ({len(coords)} cells)")

        # Select shade once at the start of this color group
        pal.select(group, shade, status_cb)
        remaining = list(coords)

        # ── Region bucket-fill (zip-style with local-base sampling + spill check) ──
        if bucket_regions_ok and not disable_region_fill and len(coords) >= opts.region_min:
            comp_set = set(map(tuple, coords))
            comps = _connected_components(coords)
            bucketed: set = set()
            settle = max(0.05, opts.verify_settle_s)

            for comp in comps:
                if stop_painting or (should_stop and should_stop()): break
                if len(comp) < opts.region_min: continue
                bnd, interior_pt = _boundary_and_interior(comp)
                if interior_pt is None: continue

                if status_cb: status_cb(f"  ↳ Region: outline {len(bnd)}, fill {len(comp)}")

                # Close panel before painting canvas
                if pal.cal.shade_panel_back_xy:
                    _pal_tap(*pal.cal.shade_panel_back_xy, opts)

                # Local base sampler: use neighbor just outside component
                local_base_cache: Dict = {}
                def _local_base(px, py, _cs=comp_set, _cr=canvas_rect, _c2s=c2s, _br=base_rgb, _cache=local_base_cache):
                    key = (px, py)
                    if key in _cache: return _cache[key]
                    for nx, ny in ((px-1,py),(px+1,py),(px,py-1),(px,py+1)):
                        if (nx,ny) not in _cs:
                            try:
                                cx, cy = _c2s(nx, ny)
                                rgb = _get_pixel_fast(cx, cy)
                                if _br is not None and _d2(rgb, _br) <= base_tol2:
                                    _cache[key] = rgb; return rgb
                            except Exception: pass
                    _cache[key] = _br; return _br

                # Paint outline
                outline_opts = PaintOptions(**{**opts.__dict__, "enable_drag": False,
                                               "rapid_click_strokes": False, "double_paint": False,
                                               "hold_s": max(opts.hold_s, 0.02),
                                               "after_s": max(opts.after_s, 0.02)})
                if pp: _tap(*pp, outline_opts)
                _paint_runs(
                    bnd, c2s, outline_opts, should_stop,
                    canvas_rect=canvas_rect, gw=gw, gh=gh,
                )
                time.sleep(settle)

                # Second outline pass for large or low-contrast regions
                do_second = False
                if len(comp) >= opts.region_min * 2:
                    if base_rgb is None: do_second = True
                    else:
                        close_thresh = max(60, opts.verify_tol * 2)
                        do_second = _d2(shade_rgb, base_rgb) <= close_thresh**2
                if do_second:
                    _paint_runs(
                        bnd, c2s, outline_opts, should_stop,
                        canvas_rect=canvas_rect, gw=gw, gh=gh,
                    )
                    time.sleep(settle)

                # Verify outline before bucket
                outline_ok = _verify_outline(
                    bnd, c2s, canvas_rect, shade_rgb, base_rgb,
                    opts, pal, group, shade, should_stop, status_cb, _local_base
                )

                # Allow cautious fill for large high-contrast regions even if outline failed
                contrast2 = _d2(shade_rgb, base_rgb) if base_rgb else 0
                allow_cautious = (not outline_ok and base_rgb is not None
                                  and len(comp) >= max(opts.region_min, 600)
                                  and contrast2 >= 120*120)

                if not outline_ok and not allow_cautious:
                    if status_cb: status_cb("  ⚠ Outline not verified; skipping bucket (will paint normally)")
                    continue

                # Find interior components and bucket-fill each
                interior_comps = _interior_components(comp, bnd)
                if not interior_comps: continue

                if bp: _tap(*bp, opts)
                filled_cells = set(map(tuple, bnd))
                spill_detected = False
                filled_any = False

                for sub in interior_comps:
                    if stop_painting or (should_stop and should_stop()): break
                    if not sub: continue
                    fx, fy = sub[0]
                    _tap(*c2s(fx, fy), opts)
                    if settle > 0: time.sleep(settle)

                    # Check fill registered (cell should no longer look like base)
                    if base_rgb is not None:
                        cx, cy = c2s(fx, fy)
                        actual = _get_pixel_fast(cx, cy)
                        if _d2(actual, base_rgb) <= base_tol2: continue  # fill didn't take

                    filled_any = True
                    filled_cells |= set(map(tuple, sub))

                    # Spill check: only for cautious fill (outline wasn't fully verified).
                    # For normal confirmed region fills, the verified outline is the safeguard.
                    if allow_cautious:
                        comp_tuple_set = set(map(tuple, comp))
                        stride = max(1, len(bnd)//7); samples = 0; changed = 0
                        for bi in range(0, len(bnd), stride):
                            if samples >= 6: break
                            bx2, by2 = bnd[bi]
                            for nx, ny in ((bx2-1,by2),(bx2+1,by2),(bx2,by2-1),(bx2,by2+1)):
                                if (nx,ny) not in comp_tuple_set:
                                    try:
                                        cx2, cy2 = c2s(nx, ny)
                                        a2 = _get_pixel_fast(cx2, cy2)
                                        samples += 1
                                        if base_rgb and _d2(a2, base_rgb) > base_tol2: changed += 1
                                    except Exception: pass
                                    break
                        if samples > 0 and changed > 0:
                            spill_detected = True
                            if status_cb: status_cb(f"  ⚠ Spill detected! Disabling region fill.")
                            break

                if pp: _tap(*pp, opts)

                if filled_any:
                    bucketed |= filled_cells
                    _done(list(filled_cells))

                if spill_detected:
                    disable_region_fill = True; break

            remaining = [p for p in coords if tuple(p) not in bucketed]

        # ── Normal per-cell painting ────────────────────────────────────────────
        if remaining:
            # Assert paint tool before pixel painting (bucket-fill may have left us
            # in bucket mode; this ensures we are always in paint mode).
            if pp: _tap(*pp, opts, extra_delay=0.02)

            # Streaming verify — zip ``paint.py`` pattern: one cell at a time from the
            # queue, sample at ``_cell_center``, double-tap repair (not batched mss).
            verify_queue: deque = deque()
            verify_settle_s = max(0.0, float(opts.verify_settle_s))
            verify_settle_s = min(0.10, verify_settle_s)  # zip clamps streaming settle
            lag = opts.verify_stream_lag

            def flush_verify(force=False, max_steps=1):
                if not opts.verify_streaming:
                    return
                steps = 0
                while verify_queue and (force or len(verify_queue) > lag):
                    if not force and steps >= max(1, int(max_steps)):
                        break
                    if should_stop and should_stop():
                        return
                    px, py, t_painted = verify_queue[0]
                    if verify_settle_s > 0:
                        ready_in = (float(t_painted) + verify_settle_s) - time.time()
                        if ready_in > 0:
                            if not force:
                                break
                            if not _sleep_with_stop(min(0.02, ready_in), should_stop):
                                return
                            continue
                    px, py, _ = verify_queue.popleft()
                    cx, cy = _cell_center(canvas_rect, gw, gh, int(px), int(py))
                    try:
                        actual = _get_pixel_fast(cx, cy)
                    except Exception:
                        steps += 1
                        continue
                    if _d2(actual, shade_rgb) <= tol2:
                        steps += 1
                        continue
                    # Zip: mismatch → double tap same cell (shade already selected).
                    _tap(cx, cy, opts)
                    _tap(cx, cy, opts, extra_delay=0.01)
                    if progress_cb:
                        progress_cb(px, py, done, total)
                    steps += 1

            def on_painted(px, py):
                nonlocal done
                done += 1
                if progress_cb: progress_cb(px, py, done, total)
                if opts.verify_streaming:
                    verify_queue.append((px, py, time.time()))
                    backlog = len(verify_queue) - lag
                    steps = 6 if backlog > 60 else (3 if backlog > 30 else 1)
                    flush_verify(force=False, max_steps=steps)

            _paint_runs(
                remaining, c2s, opts, should_stop, progress_cb=on_painted,
                canvas_rect=canvas_rect, gw=gw, gh=gh,
            )

            if opts.verify_streaming:
                flush_verify(force=True)
            # Zip: if streaming is on, post-pass ``_verify_and_repair_color_group`` is
            # skipped (streaming already repaired per cell). If streaming is off, run it.
            if (not opts.verify_streaming) and not (
                stop_painting or (should_stop and should_stop())
            ):
                def _verify_progress(px, py, _done=done, _total=total):
                    if progress_cb:
                        progress_cb(px, py, _done, _total)
                _verify_and_repair_color_group(
                    remaining,
                    canvas_rect,
                    gw,
                    gh,
                    shade_rgb,
                    group,
                    shade,
                    pal,
                    opts,
                    should_stop=should_stop,
                    status_cb=status_cb,
                    progress_cb=_verify_progress,
                )

    if status_cb: status_cb("✅  Painting complete!")

# ══════════════════════════════════════════════════════════════════════════════
# IN-GAME CLICK CAPTURE OVERLAY
# ══════════════════════════════════════════════════════════════════════════════
def _capture_point(parent_root, instruction, callback):
    import tkinter as tk
    parent_root.withdraw(); time.sleep(0.25)
    ss = ImageGrab.grab()
    try: dim = ImageEnhance.Brightness(ss).enhance(0.70)
    except Exception: dim = ss

    ov=tk.Toplevel(parent_root); ov.attributes("-fullscreen",True); ov.attributes("-topmost",True)
    ov.overrideredirect(True)
    cv=tk.Canvas(ov,cursor="crosshair",bg="#1a1a2a",highlightthickness=0)
    cv.pack(fill=tk.BOTH,expand=True)
    try:
        from PIL import ImageTk
        bg_img=ImageTk.PhotoImage(dim); cv.create_image(0,0,anchor="nw",image=bg_img)
    except Exception: pass
    sw = ov.winfo_screenwidth()
    cv.create_rectangle(0,0,sw,54,fill="#1a1a2a",outline="")
    cv.create_text(sw//2,14,text=instruction,fill="#FFFF00",font=("Consolas",14,"bold"),anchor="n")
    cv.create_text(sw//2,42,text="Right-click or ESC to cancel",fill="#888888",font=("Consolas",10),anchor="n")
    xhair=[None,None,None,None]
    def on_move(e):
        for item in xhair:
            try: cv.delete(item)
            except Exception: pass
        x,y=e.x,e.y
        xhair[0]=cv.create_line(x-20,y,x+20,y,fill="#00FFFF",width=2)
        xhair[1]=cv.create_line(x,y-20,x,y+20,fill="#00FFFF",width=2)
        xhair[2]=cv.create_oval(x-5,y-5,x+5,y+5,outline="#00FFFF",width=2)
        xhair[3]=cv.create_text(x+16,y-16,text=f"({x},{y})",fill="#FFFF00",font=("Consolas",9))
    def on_click(e):
        ov.destroy(); parent_root.deiconify()
        try: rgb=_get_pixel_fast(e.x,e.y)
        except Exception: rgb=(0,0,0)
        callback((e.x,e.y),rgb)
    def on_cancel(e=None): ov.destroy(); parent_root.deiconify()
    cv.bind("<Motion>",on_move); cv.bind("<Button-1>",on_click)
    cv.bind("<Button-3>",on_cancel); ov.bind("<Escape>",on_cancel)
    ov.wait_window(ov)

# ══════════════════════════════════════════════════════════════════════════════
# GUI — Professional redesign  v4.1
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# PROFESSIONAL GUI  — v4.1
# ══════════════════════════════════════════════════════════════════════════════
def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    # ── Design tokens ─────────────────────────────────────────────────────────
    BG      = "#080c14"   # root background — deep navy-black
    SURF    = "#0c1220"   # main surface
    CARD    = "#101828"   # card
    CARD2   = "#152033"   # elevated card
    INSET   = "#070a11"   # inset / text widgets
    EDGE    = "#1a2638"   # borders
    EDGE2   = "#223048"   # active borders
    HOVER   = "#1c2e48"   # hover surface

    ACCENT  = "#e03e5a";  ACCENT_H = "#f04d6a"
    BLUE    = "#2f6fd4";  BLUE_H   = "#3d7eeb"
    GREEN   = "#0f9e74";  GREEN_H  = "#10b886"
    AMBER   = "#d4890a";  AMBER_H  = "#f59e0b"
    RED     = "#b42020";  RED_H    = "#d43030"

    TEXT    = "#ccd9ec"
    DIM     = "#4e6585"
    MUTED   = "#253548"

    FONT = "Segoe UI"
    MONO = "Consolas"
    FN  = (FONT, 9);          FB  = (FONT, 9,  "bold")
    FH  = (FONT, 10, "bold"); FL  = (FONT, 11, "bold")
    FT  = (FONT, 13, "bold"); FS  = (FONT, 8)
    FSB = (FONT, 8,  "bold"); FM  = (MONO, 9)
    FXL = (FONT, 16, "bold")

    # ── State ─────────────────────────────────────────────────────────────────
    S = {"img_path": None, "ratio": "1:1", "detail_idx": 1, "gw": 50, "gh": 50,
         "draw_map": None, "canvas_det": None, "cal": CalibData(),
         "preview_img": None, "thread": None}

    # ── Design helpers ─────────────────────────────────────────────────────────
    def hov(w, on, off):
        w.bind("<Enter>", lambda _: w.config(bg=on))
        w.bind("<Leave>", lambda _: w.config(bg=off))

    def mk_btn(p, t, cmd, bg=BLUE, fg=TEXT, hbg=None, font=None, **kw):
        kw.setdefault("relief", "flat"); kw.setdefault("cursor", "hand2")
        kw.setdefault("borderwidth", 0); kw.setdefault("highlightthickness", 0)
        kw.setdefault("padx", 14); kw.setdefault("pady", 8)
        _hbg = hbg or bg
        kw["activebackground"] = _hbg; kw["activeforeground"] = fg
        b = tk.Button(p, text=t, command=cmd, bg=bg, fg=fg, font=font or FB, **kw)
        if hbg: hov(b, hbg, bg)
        return b

    def lbl(p, t, fg=TEXT, font=None, bg=None, **kw):
        return tk.Label(p, text=t, bg=bg or p["bg"], fg=fg, font=font or FN, **kw)

    def div(p, h=1, c=EDGE):
        f = tk.Frame(p, bg=c, height=h)
        return f

    def vdiv(p, w=1, c=EDGE):
        return tk.Frame(p, bg=c, width=w)

    def card_frame(p, bg=CARD, px=14, py=12, **kw):
        return tk.Frame(p, bg=bg, padx=px, pady=py, **kw)

    def tag_label(p, text, bg=CARD2, fg=DIM):
        """Monospace tag / badge."""
        return tk.Label(p, text=text, bg=bg, fg=fg, font=FM,
                        padx=6, pady=2, relief="flat")

    def pill(p, text, color, size=8):
        """Colored status pill."""
        f = tk.Frame(p, bg=color, padx=5, pady=1)
        tk.Label(f, text=text, bg=color, fg="white", font=(FONT, size, "bold")).pack()
        return f

    def dot_label(p, text, status):
        """Row: colored dot + label. status: True/False/'warn'."""
        clr = GREEN if status is True else (AMBER if status == "warn" else DIM)
        row = tk.Frame(p, bg=p["bg"])
        tk.Frame(row, bg=clr, width=7, height=7).pack(side=tk.LEFT, padx=(0, 7), pady=5)
        lbl(row, text, fg=TEXT if status else DIM, bg=p["bg"]).pack(side=tk.LEFT)
        return row, clr

    def step_badge(p, n, done=False, active=False):
        """Circular step number badge using Canvas."""
        sz = 22
        c = tk.Canvas(p, width=sz, height=sz, bg=p["bg"], highlightthickness=0)
        fill = GREEN if done else (BLUE if active else MUTED)
        c.create_oval(1, 1, sz-1, sz-1, fill=fill, outline="")
        c.create_text(sz//2, sz//2, text="✓" if done else str(n),
                      fill="white" if (done or active) else DIM,
                      font=(FONT, 8, "bold"))
        return c

    def section_head(p, text, color=BLUE):
        row = tk.Frame(p, bg=p["bg"])
        tk.Frame(row, bg=color, width=3, height=18).pack(side=tk.LEFT, padx=(0, 9))
        lbl(row, text, fg=TEXT, font=FH, bg=p["bg"]).pack(side=tk.LEFT, anchor="center")
        return row

    # ── Root window ────────────────────────────────────────────────────────────
    root = tk.Tk()
    root.title("Heartopia Painter  —  v4.2  ·  GLouTuny")
    root.configure(bg=BG)
    root.geometry("1360x860")
    root.minsize(1100, 720)
    root.resizable(True, True)

    # Dark title bar on Windows 10/11
    if sys.platform == "win32":
        try:
            import ctypes
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35, ctypes.byref(ctypes.c_int(0x140C0A)), 4)
        except Exception:
            pass

    # ── TTK styles ─────────────────────────────────────────────────────────────
    sty = ttk.Style(root)
    sty.theme_use("clam")
    sty.configure("Paint.Horizontal.TProgressbar",
                  troughcolor=INSET, background=ACCENT,
                  lightcolor=ACCENT, darkcolor=ACCENT, thickness=16, relief="flat")
    sty.configure("Cal.Horizontal.TProgressbar",
                  troughcolor=INSET, background=GREEN,
                  lightcolor=GREEN, darkcolor=GREEN, thickness=5)
    sty.configure("TNotebook", background=SURF, borderwidth=0, tabmargins=0)
    sty.configure("TNotebook.Tab", background=SURF, foreground=DIM,
                  padding=[22, 11], font=FB, borderwidth=0)
    sty.map("TNotebook.Tab",
            background=[("selected", CARD), ("active", CARD2)],
            foreground=[("selected", TEXT), ("active", TEXT)])
    sty.configure("Vertical.TScrollbar", background=CARD2, troughcolor=INSET,
                  borderwidth=0, arrowcolor=DIM, arrowsize=12, relief="flat")
    sty.map("Vertical.TScrollbar", background=[("active", EDGE2)])
    sty.configure("TCombobox", fieldbackground=CARD2, background=CARD2,
                  foreground=TEXT, arrowcolor=DIM, borderwidth=0,
                  selectbackground=BLUE, selectforeground=TEXT, relief="flat")
    sty.map("TCombobox", fieldbackground=[("readonly", CARD2)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", BLUE)])

    # ── App header ─────────────────────────────────────────────────────────────
    header = tk.Frame(root, bg=CARD, height=50)
    header.pack(fill=tk.X)
    header.pack_propagate(False)
    tk.Label(header, text="⬡  HEARTOPIA PAINTER", bg=CARD, fg=ACCENT,
             font=(FONT, 13, "bold")).pack(side=tk.LEFT, padx=20, pady=13)
    vdiv(header, c=EDGE2).pack(side=tk.LEFT, fill=tk.Y, pady=12)
    lbl(header, "Automated Canvas Painter", fg=DIM, bg=CARD).pack(
        side=tk.LEFT, padx=14, pady=13)

    # Version badge top-right
    vf = tk.Frame(header, bg=CARD)
    vf.pack(side=tk.RIGHT, padx=16)
    tk.Label(vf, text=" v4.2 ", bg=EDGE2, fg=DIM, font=FSB, padx=6, pady=3).pack()

    div(root, c=EDGE).pack(fill=tk.X)

    # ── Notebook ───────────────────────────────────────────────────────────────
    nb = ttk.Notebook(root)
    nb.pack(fill=tk.BOTH, expand=True)

    t1 = tk.Frame(nb, bg=SURF)
    t2 = tk.Frame(nb, bg=SURF)
    t3 = tk.Frame(nb, bg=SURF)
    nb.add(t1, text="   Paint   ")
    nb.add(t2, text="   Calibration   ")
    nb.add(t3, text="   Options   ")

    # ── Status bar ─────────────────────────────────────────────────────────────
    div(root, c=EDGE).pack(fill=tk.X)
    sbar = tk.Frame(root, bg=INSET, height=30)
    sbar.pack(fill=tk.X)
    sbar.pack_propagate(False)
    sbar_dot = tk.Frame(sbar, bg=DIM, width=8, height=8)
    sbar_dot.pack(side=tk.LEFT, padx=(14, 7), pady=11)
    sbar_var = tk.StringVar(value="Ready")
    sbar_lbl = tk.Label(sbar, textvariable=sbar_var, bg=INSET, fg=DIM,
                        font=FS, anchor="w")
    sbar_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
    lbl(sbar, "F10  Pause/Resume   ·   F12  Stop", fg=MUTED, bg=INSET,
        font=FS).pack(side=tk.RIGHT, padx=14)

    def set_sbar(msg, color=None):
        sbar_var.set(msg)
        c = color or DIM
        sbar_dot.config(bg=c)
        sbar_lbl.config(fg=c)

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1 — PAINT
    # ══════════════════════════════════════════════════════════════════════════
    # Left sidebar + right content
    sidebar = tk.Frame(t1, bg=CARD, width=348)
    sidebar.pack(side=tk.LEFT, fill=tk.Y)
    sidebar.pack_propagate(False)
    vdiv(t1, c=EDGE).pack(side=tk.LEFT, fill=tk.Y)
    content_area = tk.Frame(t1, bg=SURF)
    content_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # ── Sidebar header ─────────────────────────────────────────────────────────
    sb_hdr = tk.Frame(sidebar, bg=CARD2, height=42)
    sb_hdr.pack(fill=tk.X)
    sb_hdr.pack_propagate(False)
    lbl(sb_hdr, "SETUP CHECKLIST", fg=DIM, font=FSB, bg=CARD2).pack(
        side=tk.LEFT, padx=16, pady=13)

    def _sb_sep():
        div(sidebar, c=EDGE).pack(fill=tk.X)

    # ── Step builder helper ────────────────────────────────────────────────────
    def _step_block(n, title, color=BLUE):
        """Returns (outer_frame, body_frame, badge_canvas, status_var, status_lbl)."""
        outer = tk.Frame(sidebar, bg=CARD, padx=14, pady=10)
        outer.pack(fill=tk.X)

        # Title row
        tr = tk.Frame(outer, bg=CARD)
        tr.pack(fill=tk.X)
        badge = step_badge(tr, n, done=False, active=False)
        badge.pack(side=tk.LEFT, padx=(0, 9))
        lbl(tr, title.upper(), fg=DIM, font=FSB, bg=CARD).pack(
            side=tk.LEFT, anchor="center")

        sv = tk.StringVar(value="—")
        sl = tk.Label(tr, textvariable=sv, bg=CARD, fg=DIM, font=FS, anchor="e")
        sl.pack(side=tk.RIGHT, anchor="center")

        body = tk.Frame(outer, bg=CARD)
        body.pack(fill=tk.X, pady=(6, 0), padx=(30, 0))
        return outer, body, badge, sv, sl

    # ─── Step 1: Image ─────────────────────────────────────────────────────────
    _sb_sep()
    s1_frame, s1_body, s1_badge, s1_var, s1_slbl = _step_block(1, "Image")
    img_name_var = tk.StringVar(value="No image selected")
    tk.Label(s1_body, textvariable=img_name_var, bg=CARD, fg=DIM, font=FS,
             wraplength=300, justify=tk.LEFT).pack(anchor="w")
    btn_row_img = tk.Frame(s1_body, bg=CARD)
    btn_row_img.pack(anchor="w", pady=(6, 0))

    def browse_image():
        p = filedialog.askopenfilename(title="Select Image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All", "*.*")])
        if not p: return
        S["img_path"] = p
        name = os.path.basename(p)
        img_name_var.set(name)
        s1_var.set("✓")
        s1_slbl.config(fg=GREEN)
        _refresh_step_badges()
        if "rf" in S: S["rf"]()
        prog_var.set(0); cells_var.set("")

    mk_btn(btn_row_img, "Browse Image…", browse_image,
           bg=CARD2, fg=TEXT, hbg=HOVER, pady=6, padx=10).pack(side=tk.LEFT)

    # ─── Step 2: Grid ──────────────────────────────────────────────────────────
    _sb_sep()
    s2_frame, s2_body, s2_badge, s2_var, s2_slbl = _step_block(2, "Grid")
    ratio_var = tk.StringVar(value="1:1")
    detail_var = tk.StringVar(value="Medium")
    grid_info_var = tk.StringVar(value="50 × 50  =  2,500 cells")

    rg_row = tk.Frame(s2_body, bg=CARD)
    rg_row.pack(fill=tk.X, pady=(0, 4))
    lbl(rg_row, "Ratio", fg=DIM, font=FS, bg=CARD).pack(side=tk.LEFT, padx=(0, 6))
    ratio_cb = ttk.Combobox(rg_row, textvariable=ratio_var,
                             values=list(GRID_DIMENSIONS.keys()),
                             state="readonly", width=6)
    ratio_cb.pack(side=tk.LEFT, padx=(0, 10))
    lbl(rg_row, "Detail", fg=DIM, font=FS, bg=CARD).pack(side=tk.LEFT, padx=(0, 6))
    detail_cb = ttk.Combobox(rg_row, textvariable=detail_var,
                              values=DETAIL_NAMES, state="readonly", width=10)
    detail_cb.pack(side=tk.LEFT)

    gi_lbl = lbl(s2_body, "", fg=DIM, font=FM)
    gi_lbl.pack(anchor="w")
    gi_lbl.config(textvariable=grid_info_var)

    def on_grid(*_):
        r = ratio_var.get()
        di = DETAIL_NAMES.index(detail_var.get())
        gw, gh = GRID_DIMENSIONS[r][di]
        S.update(ratio=r, detail_idx=di, gw=gw, gh=gh)
        grid_info_var.set(f"{gw} × {gh}  =  {gw*gh:,} cells")
        s2_var.set(f"{gw}×{gh}")
        s2_slbl.config(fg=GREEN)
        try:
            _refresh_step_badges()
        except NameError:
            pass
        if "rf" in S: S["rf"]()

    ratio_cb.bind("<<ComboboxSelected>>", on_grid)
    detail_cb.bind("<<ComboboxSelected>>", on_grid)
    on_grid()

    # ─── Step 3: Canvas ────────────────────────────────────────────────────────
    _sb_sep()
    s3_frame, s3_body, s3_badge, s3_var, s3_slbl = _step_block(3, "Canvas")
    canvas_info_var = tk.StringVar(value="Not detected")
    ck_lbl = lbl(s3_body, "", fg=DIM, font=FS)
    ck_lbl.pack(anchor="w")
    ck_lbl.config(textvariable=canvas_info_var)

    det_row = tk.Frame(s3_body, bg=CARD)
    det_row.pack(anchor="w", pady=(6, 0))

    def do_detect():
        gw = S["gw"]; gh = S["gh"]
        canvas_info_var.set("Detecting…")
        root.update(); root.withdraw(); time.sleep(0.5)
        try:
            r = detect_canvas_auto(gw, gh)
        finally:
            root.deiconify()
        if r:
            build_cell_to_screen(r, gw, gh)
            S["canvas_det"] = r
            canvas_info_var.set(f"Auto  ({r['snip_x']},{r['snip_y']})  {r['snip_w']}×{r['snip_h']}px")
            ck_lbl.config(fg=GREEN)
            s3_var.set("✓ Auto")
            s3_slbl.config(fg=GREEN)
            _refresh_step_badges()
            root.withdraw()
            show_canvas_overlay(r, gw, gh, duration=4.0, main_root=root)
            root.deiconify()
        else:
            canvas_info_var.set("Failed — try Manual Drag")
            ck_lbl.config(fg=ACCENT)
            s3_var.set("✗")
            s3_slbl.config(fg=ACCENT)
            messagebox.showwarning("Detection Failed",
                "Stripe auto-detect failed.\nMake sure the canvas shows unpainted stripes.\n"
                "Use Manual Drag to select it manually.")

    def do_manual():
        gw = S["gw"]; gh = S["gh"]
        root.withdraw(); time.sleep(0.35)
        try:
            r = overlay_drag_select(gw, gh, main_root=root)
        finally:
            root.deiconify()
        if r:
            lx, ty, rw, rh = r
            ps = min(CANVAS_W/gw, CANVAS_H/gh)
            cv_d = dict(snip_x=lx, snip_y=ty, snip_w=rw, snip_h=rh,
                        scale=rw/(ps*gw), method="overlay")
            build_cell_to_screen(cv_d, gw, gh)
            S["canvas_det"] = cv_d
            canvas_info_var.set(f"Manual  ({lx},{ty})  {rw}×{rh}px")
            ck_lbl.config(fg=GREEN)
            s3_var.set("✓ Manual")
            s3_slbl.config(fg=GREEN)
            _refresh_step_badges()

    mk_btn(det_row, "Auto Detect", do_detect, bg=BLUE, fg=TEXT,
           hbg=BLUE_H, pady=6, padx=10).pack(side=tk.LEFT, padx=(0, 6))
    mk_btn(det_row, "Manual Drag", do_manual, bg=CARD2, fg=TEXT,
           hbg=HOVER, pady=6, padx=10).pack(side=tk.LEFT)

    # ─── Step 4: Calibration status ────────────────────────────────────────────
    _sb_sep()
    s4_frame, s4_body, s4_badge, s4_var, s4_slbl = _step_block(4, "Calibration")
    cal_info_var = tk.StringVar(value="Not calibrated")
    cal_badge_lbl = lbl(s4_body, "", fg=DIM, font=FS)
    cal_badge_lbl.pack(anchor="w")
    cal_badge_lbl.config(textvariable=cal_info_var)
    cal_goto_btn = mk_btn(s4_body, "Open Calibration →",
                          lambda: nb.select(1),
                          bg=CARD2, fg=DIM, hbg=HOVER, pady=5, padx=10, font=FS)
    cal_goto_btn.pack(anchor="w", pady=(5, 0))

    cal_status_labels = []  # populated after tab2 setup

    def refresh_cal_status():
        """Delegates to _do_refresh_cal_status once it is defined (after tab2 setup)."""
        try: _do_refresh_cal_status()
        except NameError: pass  # called before tab2 widgets exist — safe to skip

    def _refresh_step_badges():
        """Update step badge colors based on state."""
        states = [
            bool(S.get("img_path")),
            True,  # grid always set
            bool(S.get("canvas_det")),
            S["cal"].is_complete,
        ]
        badges = [s1_badge, s2_badge, s3_badge, s4_badge]
        for badge, done in zip(badges, states):
            badge.delete("all")
            fill = GREEN if done else MUTED
            badge.create_oval(1, 1, 21, 21, fill=fill, outline="")
            badge.create_text(11, 11, text="✓" if done else str(badges.index(badge)+1),
                              fill="white" if done else DIM,
                              font=(FONT, 8, "bold"))

    # Bottom of sidebar — spacer
    tk.Frame(sidebar, bg=CARD).pack(fill=tk.BOTH, expand=True)
    _sb_sep()
    ready_bar = tk.Frame(sidebar, bg=CARD2, padx=14, pady=10)
    ready_bar.pack(fill=tk.X)
    ready_var = tk.StringVar(value="4 steps required before painting")
    ready_lbl = tk.Label(ready_bar, textvariable=ready_var, bg=CARD2, fg=DIM,
                         font=FS, wraplength=280, justify=tk.LEFT)
    ready_lbl.pack(anchor="w")

    # ── Right content: Preview + progress + controls ───────────────────────────
    # Preview area
    prev_card = tk.Frame(content_area, bg=SURF, padx=12, pady=10)
    prev_card.pack(fill=tk.BOTH, expand=True)

    prev_hdr = tk.Frame(prev_card, bg=SURF)
    prev_hdr.pack(fill=tk.X, pady=(0, 6))
    lbl(prev_hdr, "PREVIEW", fg=DIM, font=FSB, bg=SURF).pack(side=tk.LEFT)
    lbl(prev_hdr, "scroll to zoom  ·  drag to pan", fg=MUTED, font=FS,
        bg=SURF).pack(side=tk.LEFT, padx=12)

    zoom_row = tk.Frame(prev_hdr, bg=SURF)
    zoom_row.pack(side=tk.RIGHT)
    preview_zoom_var = tk.DoubleVar(value=1.0)
    S["preview_pan"] = [0, 0]
    _pan_start = [None, None]

    PREV_W, PREV_H = 820, 440

    def _clamp_pan(px, py, nw, nh):
        mx = max(0.0, (nw - PREV_W) / 2.0 + 2)
        my = max(0.0, (nh - PREV_H) / 2.0 + 2)
        return (max(-mx, min(mx, float(px))), max(-my, min(my, float(py))))

    def _redraw_preview():
        prev = S.get("preview_pil")
        if prev is None: return
        zoom = max(0.25, min(4.0, preview_zoom_var.get()))
        preview_zoom_var.set(zoom)
        pw, ph = prev.size
        sc = min(PREV_W/pw, PREV_H/ph, 1.0) * zoom
        nw, nh = max(1, int(pw*sc)), max(1, int(ph*sc))
        S["preview_display_size"] = (nw, nh)
        pan_x, pan_y = S["preview_pan"]
        pan_x, pan_y = _clamp_pan(pan_x, pan_y, nw, nh)
        S["preview_pan"] = [pan_x, pan_y]
        thumb = prev.resize((nw, nh), Image.Resampling.NEAREST)
        tki = ImageTk.PhotoImage(thumb)
        S["preview_img"] = tki
        x0 = round(PREV_W/2 - nw/2 + pan_x)
        y0 = round(PREV_H/2 - nh/2 + pan_y)
        pv_canvas.delete("all")
        pv_canvas.create_image(x0, y0, image=tki, anchor="nw")
        zoom_lbl.config(text=f"{int(zoom*100)}%")

    def _zoom_in(): preview_zoom_var.set(min(4.0, preview_zoom_var.get() * 1.25)); _redraw_preview()
    def _zoom_out(): preview_zoom_var.set(max(0.25, preview_zoom_var.get() / 1.25)); _redraw_preview()
    def _zoom_reset(): preview_zoom_var.set(1.0); S["preview_pan"] = [0, 0]; _redraw_preview()

    zoom_lbl = lbl(zoom_row, "100%", fg=DIM, font=FS, bg=SURF)
    zoom_lbl.pack(side=tk.LEFT, padx=6)
    mk_btn(zoom_row, "−", _zoom_out, bg=CARD2, fg=DIM, hbg=HOVER,
           padx=8, pady=4, font=FB).pack(side=tk.LEFT, padx=(0, 2))
    mk_btn(zoom_row, "+", _zoom_in, bg=CARD2, fg=DIM, hbg=HOVER,
           padx=8, pady=4, font=FB).pack(side=tk.LEFT, padx=(0, 2))
    mk_btn(zoom_row, "1:1", _zoom_reset, bg=CARD2, fg=DIM, hbg=HOVER,
           padx=6, pady=4, font=FS).pack(side=tk.LEFT)

    # Canvas widget for preview
    pv_canvas = tk.Canvas(prev_card, bg=INSET, highlightthickness=1,
                          highlightbackground=EDGE, width=PREV_W, height=PREV_H,
                          cursor="fleur")
    pv_canvas.pack()

    def _on_press(e): _pan_start[:] = [e.x, e.y, S["preview_pan"][0], S["preview_pan"][1]]
    def _on_drag(e):
        if _pan_start[0] is None: return
        nw, nh = S.get("preview_display_size", (0, 0))
        if not (nw and nh): return
        px, py = _clamp_pan(_pan_start[2]+(e.x-_pan_start[0]),
                             _pan_start[3]+(e.y-_pan_start[1]), nw, nh)
        S["preview_pan"] = [px, py]; _redraw_preview()
    def _on_release(e): _pan_start[0] = None
    def _on_scroll(e):
        if e.delta > 0: _zoom_in()
        else: _zoom_out()

    pv_canvas.bind("<ButtonPress-1>", _on_press)
    pv_canvas.bind("<B1-Motion>", _on_drag)
    pv_canvas.bind("<ButtonRelease-1>", _on_release)
    pv_canvas.bind("<MouseWheel>", _on_scroll)

    # Empty state label
    empty_lbl = lbl(prev_card, "Browse an image to see the palette preview",
                    fg=MUTED, font=FN, bg=INSET)
    pv_canvas.create_window(PREV_W//2, PREV_H//2, window=empty_lbl)

    status_var = tk.StringVar(value="Ready")
    cells_var = tk.StringVar(value="")

    def refresh_preview(*_):
        ip = S["img_path"]; gw = S["gw"]; gh = S["gh"]
        if not ip or not os.path.isfile(ip): return
        try:
            empty_lbl.place_forget()
            status_var.set("Processing image…"); root.update()
            dm = process_image(ip, gw, gh); S["draw_map"] = dm
            tot = sum(len(v) for v in dm.values())
            prev = generate_preview(dm, gw, gh); S["preview_pil"] = prev
            preview_zoom_var.set(1.0); S["preview_pan"] = [0, 0]
            _redraw_preview()
            status_var.set(f"{gw}×{gh}  —  {len(dm)} colors  —  {tot:,} cells")
            set_sbar(f"Image loaded: {os.path.basename(ip)}  ({gw}×{gh}, {tot:,} cells)", GREEN)
        except Exception as e:
            status_var.set(f"Error: {e}")
            set_sbar(str(e), ACCENT)

    S["rf"] = refresh_preview

    # ── Progress + status strip ─────────────────────────────────────────────────
    div(content_area, c=EDGE).pack(fill=tk.X, pady=0)
    prog_strip = tk.Frame(content_area, bg=CARD, padx=14, pady=10)
    prog_strip.pack(fill=tk.X)

    prog_top = tk.Frame(prog_strip, bg=CARD)
    prog_top.pack(fill=tk.X, pady=(0, 6))
    st_lbl = lbl(prog_top, "", fg=TEXT, font=FB, bg=CARD)
    st_lbl.pack(side=tk.LEFT); st_lbl.config(textvariable=status_var)
    pct_lbl = lbl(prog_top, "", fg=DIM, font=FM, bg=CARD)
    pct_lbl.pack(side=tk.RIGHT)
    cells_lbl = lbl(prog_top, "", fg=DIM, font=FS, bg=CARD)
    cells_lbl.pack(side=tk.RIGHT, padx=16); cells_lbl.config(textvariable=cells_var)

    prog_var = tk.DoubleVar(value=0)
    prog_bar = ttk.Progressbar(prog_strip, variable=prog_var, maximum=100,
                                style="Paint.Horizontal.TProgressbar")
    prog_bar.pack(fill=tk.X)

    # ── Control row ────────────────────────────────────────────────────────────
    div(content_area, c=EDGE).pack(fill=tk.X)
    ctrl = tk.Frame(content_area, bg=CARD2, padx=14, pady=12)
    ctrl.pack(fill=tk.X)

    btn_row = tk.Frame(ctrl, bg=CARD2)
    btn_row.pack(side=tk.LEFT)

    # Options vars (referenced in do_start, defined in tab3 section)
    drag_v        = tk.BooleanVar(value=True)
    bucket_v      = tk.BooleanVar(value=True)
    region_v      = tk.BooleanVar(value=True)
    double_v      = tk.BooleanVar(value=False)
    stream_v      = tk.BooleanVar(value=True)
    rapid_click_v = tk.BooleanVar(value=True)
    auto_recover_v= tk.BooleanVar(value=True)
    hold_v        = tk.DoubleVar(value=0.02)
    after_v       = tk.DoubleVar(value=0.06)
    pal_v         = tk.DoubleVar(value=0.12)

    def do_start():
        global stop_painting, pause_painting
        missing = []
        if not S["img_path"]: missing.append("• Select an image (Step 1)")
        if not S["canvas_det"]: missing.append("• Detect the canvas (Step 3)")
        if not S["cal"].is_complete: missing.append("• Complete calibration (Calibration tab)")
        if missing:
            messagebox.showwarning("Not Ready",
                "Cannot start painting:\n\n" + "\n".join(missing))
            return
        if S["draw_map"] is None: refresh_preview()
        if not S["draw_map"]:
            messagebox.showwarning("Error", "Could not process the image."); return
        stop_painting = False; pause_painting = False; _pause_event.set()
        opts = PaintOptions(
            hold_s=hold_v.get(), after_s=after_v.get(),
            pal_hold_s=pal_v.get(), pal_move_s=pal_v.get()*0.7,
            pal_after_s=pal_v.get()*0.7, color_settle=pal_v.get()*0.5,
            verify_settle_s=0.05, verify_max_passes=10,
            verify_streaming=stream_v.get(), verify_stream_lag=10,
            verify_auto_recover=auto_recover_v.get(), verify_recover_after=2,
            enable_drag=drag_v.get(), rapid_click_strokes=rapid_click_v.get(),
            double_paint=double_v.get(),
            bucket_min=50 if bucket_v.get() else 999999,
            bucket_regions=region_v.get(), region_min=200,
        )
        dm  = S["draw_map"]; cv  = S["canvas_det"]; cal = S["cal"]
        gw  = S["gw"];       gh  = S["gh"]
        build_cell_to_screen(cv, gw, gh)
        c2s = cv["cell_to_screen"]
        prog_var.set(0)
        pal = PaletteCtrl(cal, opts)

        def on_prog(px, py, done, tot):
            pct = done / max(1, tot) * 100
            root.after(0, lambda: prog_var.set(pct))
            root.after(0, lambda: cells_var.set(f"{done:,} / {tot:,} cells"))
            root.after(0, lambda: pct_lbl.config(text=f"{pct:.1f}%"))

        def on_st(msg):
            root.after(0, lambda: status_var.set(msg))
            root.after(0, lambda: set_sbar(msg, BLUE))

        def work():
            for i in range(5, 0, -1):
                on_st(f"Starting in {i}s  —  switch to the game now")
                time.sleep(1)
                if stop_painting: return
            paint_by_color(
                dm, gw, gh, c2s, pal,
                (cv["snip_x"], cv["snip_y"], cv["snip_w"], cv["snip_h"]),
                opts, on_prog, on_st, lambda: stop_painting
            )
            root.after(0, lambda: [
                start_btn.config(state="normal"),
                stop_btn.config(state="disabled"),
                pause_btn.config(state="disabled"),
                resume_btn.config(state="disabled"),
                set_sbar("✅  Painting complete!", GREEN),
            ])

        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        pause_btn.config(state="normal")
        resume_btn.config(state="disabled")
        t = threading.Thread(target=work, daemon=True)
        S["thread"] = t; t.start()

    def do_pause():
        global pause_painting
        pause_painting = True; _pause_event.clear()
        pause_btn.config(state="disabled"); resume_btn.config(state="normal")
        status_var.set("⏸  Paused"); set_sbar("⏸  Paused", AMBER)

    def do_resume():
        global pause_painting
        pause_painting = False; _pause_event.set()
        pause_btn.config(state="normal"); resume_btn.config(state="disabled")
        status_var.set("Resumed"); set_sbar("Resumed", BLUE)

    def do_stop():
        global stop_painting
        stop_painting = True; _pause_event.set()
        pause_btn.config(state="disabled"); resume_btn.config(state="disabled")
        stop_btn.config(state="disabled"); start_btn.config(state="normal")
        status_var.set("⛔  Stopped"); set_sbar("Stopped", ACCENT)

    def do_reset():
        global stop_painting
        stop_painting = True; _pause_event.set()
        prog_var.set(0); cells_var.set(""); pct_lbl.config(text="")
        status_var.set("Ready"); set_sbar("Ready")
        start_btn.config(state="normal"); stop_btn.config(state="disabled")
        pause_btn.config(state="disabled"); resume_btn.config(state="disabled")

    start_btn = mk_btn(btn_row, "  ▶  Start Painting  ", do_start,
                        bg=ACCENT, fg="white", hbg=ACCENT_H,
                        font=(FONT, 11, "bold"), padx=18, pady=10)
    start_btn.pack(side=tk.LEFT, padx=(0, 8))

    pause_btn = mk_btn(btn_row, "⏸  Pause", do_pause,
                        bg=AMBER, fg="#1a0800", hbg=AMBER_H,
                        padx=12, pady=10, state="disabled")
    pause_btn.pack(side=tk.LEFT, padx=(0, 4))

    resume_btn = mk_btn(btn_row, "▶  Resume", do_resume,
                         bg=GREEN, fg="#001a0f", hbg=GREEN_H,
                         padx=12, pady=10, state="disabled")
    resume_btn.pack(side=tk.LEFT, padx=(0, 4))

    stop_btn = mk_btn(btn_row, "⛔  Stop", do_stop,
                       bg=RED, fg="white", hbg=RED_H,
                       padx=12, pady=10, state="disabled")
    stop_btn.pack(side=tk.LEFT, padx=(0, 8))

    mk_btn(btn_row, "↺  Reset", do_reset,
           bg=CARD, fg=DIM, hbg=HOVER, padx=10, pady=10).pack(side=tk.LEFT)

    def _sync_pause_buttons():
        if start_btn.cget("state") == "normal": return
        if pause_painting:
            pause_btn.config(state="disabled"); resume_btn.config(state="normal")
        else:
            pause_btn.config(state="normal"); resume_btn.config(state="disabled")

    def _on_f10():
        toggle_pause(); root.after(0, _sync_pause_buttons)

    try:
        import keyboard
        keyboard.add_hotkey("f10", _on_f10, suppress=False)
        keyboard.add_hotkey("f12", set_stop, suppress=False)
    except ImportError:
        pass

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2 — CALIBRATION
    # ══════════════════════════════════════════════════════════════════════════
    cal_left_w = 500
    cal_lf = tk.Frame(t2, bg=SURF, width=cal_left_w)
    cal_lf.pack(side=tk.LEFT, fill=tk.Y)
    cal_lf.pack_propagate(False)
    vdiv(t2, c=EDGE).pack(side=tk.LEFT, fill=tk.Y)
    cal_rf = tk.Frame(t2, bg=SURF)
    cal_rf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Scrollable left
    cal_canvas_w = tk.Canvas(cal_lf, bg=SURF, highlightthickness=0)
    cal_scroll_b = ttk.Scrollbar(cal_lf, orient="vertical", command=cal_canvas_w.yview)
    cal_inner = tk.Frame(cal_canvas_w, bg=SURF)
    cal_inner.bind("<Configure>",
                   lambda e: cal_canvas_w.configure(scrollregion=cal_canvas_w.bbox("all")))
    cal_win = cal_canvas_w.create_window((0, 0), window=cal_inner, anchor="nw")
    cal_canvas_w.configure(yscrollcommand=cal_scroll_b.set)
    cal_scroll_b.pack(side=tk.RIGHT, fill=tk.Y)
    cal_canvas_w.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    cal_canvas_w.bind("<Configure>", lambda e: cal_canvas_w.itemconfig(cal_win, width=e.width))
    cal_canvas_w.bind("<MouseWheel>",
                      lambda e: cal_canvas_w.yview_scroll(int(-1*(e.delta/120)), "units"))

    CWRAP = cal_left_w - 60

    def _auto_save_cal():
        try:
            with open(Path(CALIBRATION_FILE), "w") as f:
                json.dump(S["cal"].to_dict(), f, indent=2)
        except Exception: pass

    # Cal section builder
    def _cal_card(title, step=None, color=BLUE):
        f = tk.Frame(cal_inner, bg=CARD, padx=16, pady=12)
        f.pack(fill=tk.X, padx=8, pady=(6, 0))
        hr = tk.Frame(f, bg=CARD)
        hr.pack(fill=tk.X, pady=(0, 8))
        if step:
            tk.Label(hr, text=f" {step} ", bg=color, fg="white",
                     font=FSB, padx=4, pady=2).pack(side=tk.LEFT, padx=(0, 8))
        lbl(hr, title, fg=TEXT, font=FH, bg=CARD).pack(side=tk.LEFT)
        div(f, c=EDGE).pack(fill=tk.X, pady=(0, 8))
        return f

    # ── Header ─────────────────────────────────────────────────────────────────
    cal_hdr = tk.Frame(cal_inner, bg=CARD2, padx=16, pady=12)
    cal_hdr.pack(fill=tk.X, padx=8, pady=(8, 0))
    lbl(cal_hdr, "Calibration Wizard", fg=TEXT, font=FL, bg=CARD2).pack(anchor="w")
    lbl(cal_hdr, "Follow steps 1–3 in order. Each capture auto-saves.",
        fg=DIM, font=FS, bg=CARD2, wraplength=CWRAP).pack(anchor="w", pady=(4, 0))

    # Cal status summary
    cal_summary_var = tk.StringVar(value="Not calibrated")
    cal_sum_lbl = tk.Label(cal_hdr, textvariable=cal_summary_var, bg=CARD2, fg=AMBER,
                           font=FB, wraplength=CWRAP, justify=tk.LEFT)
    cal_sum_lbl.pack(anchor="w", pady=(6, 0))
    cal_status_labels.append(cal_sum_lbl)  # registered for refresh

    def _update_cal_summary():
        cal = S["cal"]
        if cal.is_complete:
            bk = " + bucket 🪣" if (cal.paint_tool_pos and cal.bucket_tool_pos) else ""
            cal_summary_var.set(f"✅  Calibration complete{bk}")
            cal_sum_lbl.config(fg=GREEN)
        else:
            if cal.main_colors and len(cal.main_colors) >= 13:
                done = sum(1 for i, mc in enumerate(cal.main_colors[:13])
                           if mc.pos and len(mc.shades) >= (5 if i == 0 else 10)
                           and all(s.pos for s in mc.shades[:(5 if i == 0 else 10)]))
                cal_summary_var.set(f"⚠  {done}/13 groups calibrated")
                cal_sum_lbl.config(fg=AMBER)
            else:
                cal_summary_var.set("⚠  Incomplete — capture all required fields below")
                cal_sum_lbl.config(fg=AMBER)

    # ── STEP 1: Shade panel buttons ─────────────────────────────────────────────
    f_sp = _cal_card("Shade Panel Buttons", step="STEP 1", color=BLUE)
    lbl(f_sp, "Capture the BACK button first (closes shade panel).\n"
        "The shades-panel open button is optional — defaults to main tile.",
        fg=DIM, font=FS, bg=CARD, wraplength=CWRAP).pack(anchor="w", pady=(0, 8))

    sp_open_var = tk.StringVar(value="Shades panel open:  (uses main tile)")
    sp_back_var = tk.StringVar(value="Shade panel back:   not captured")
    sp_open_lbl = tk.Label(f_sp, textvariable=sp_open_var, bg=CARD, fg=DIM, font=FM,
                            wraplength=CWRAP, justify=tk.LEFT)
    sp_open_lbl.pack(anchor="w")
    sp_back_lbl = tk.Label(f_sp, textvariable=sp_back_var, bg=CARD, fg=DIM, font=FM,
                            wraplength=CWRAP, justify=tk.LEFT)
    sp_back_lbl.pack(anchor="w", pady=(2, 8))

    def _ap_shade_open(pos, rgb):
        S["cal"].shade_panel_open_xy = pos
        sp_open_var.set(f"Shades panel open:  {pos}")
        sp_open_lbl.config(fg=GREEN)
        refresh_cal_status(); _auto_save_cal()

    def _ap_shade_back(pos, rgb):
        S["cal"].shade_panel_back_xy = pos
        sp_back_var.set(f"Shade panel back:   {pos}")
        sp_back_lbl.config(fg=GREEN)
        refresh_cal_status(); _auto_save_cal()

    sp_row = tk.Frame(f_sp, bg=CARD)
    sp_row.pack(anchor="w")
    mk_btn(sp_row, "Capture Back Button  ✱",
           lambda: _capture_point(root, "Click the BACK/CLOSE button that exits the shades panel",
                                  _ap_shade_back),
           bg=BLUE, fg=TEXT, hbg=BLUE_H, pady=7).pack(anchor="w", pady=(0, 6))
    mk_btn(sp_row, "Capture Shades-Panel Open  (optional)",
           lambda: _capture_point(root, "Click the button that OPENS the shades panel (optional)",
                                  _ap_shade_open),
           bg=CARD2, fg=DIM, hbg=HOVER, pady=7).pack(anchor="w")

    # ── STEP 2: Per-group calibration ───────────────────────────────────────────
    f_pg = _cal_card("Per-Group Calibration  (13 groups)", step="STEP 2", color=BLUE)
    lbl(f_pg, "Click a group, then capture its main tile and each shade button.",
        fg=DIM, font=FS, bg=CARD, wraplength=CWRAP).pack(anchor="w", pady=(0, 8))

    # Group progress bar
    pg_prog_frame = tk.Frame(f_pg, bg=CARD)
    pg_prog_frame.pack(fill=tk.X, pady=(0, 8))
    pg_prog_var = tk.DoubleVar(value=0)
    lbl(pg_prog_frame, "Groups:", fg=DIM, font=FS, bg=CARD).pack(side=tk.LEFT, padx=(0, 8))
    pg_prog_bar = ttk.Progressbar(pg_prog_frame, variable=pg_prog_var, maximum=13,
                                   style="Cal.Horizontal.TProgressbar", length=160)
    pg_prog_bar.pack(side=tk.LEFT)
    pg_prog_lbl = lbl(pg_prog_frame, "0 / 13", fg=DIM, font=FS, bg=CARD)
    pg_prog_lbl.pack(side=tk.LEFT, padx=8)

    # Group grid buttons (13 clickable group tiles)
    pg_grid_frame = tk.Frame(f_pg, bg=CARD)
    pg_grid_frame.pack(anchor="w", pady=(0, 8))
    pg_group_btns = {}

    _cal_group_grid_ref = [None]  # ref to refresh function

    def _refresh_cal_group_grid():
        cal = S["cal"]
        done_count = 0
        for g in range(1, 14):
            if g > len(cal.main_colors): continue
            mc = cal.main_colors[g-1]
            need = 5 if g == 1 else 10
            done = mc.pos and len(mc.shades) >= need and all(
                s.pos for s in mc.shades[:need])
            if done: done_count += 1
            if g in pg_group_btns:
                b = pg_group_btns[g]
                b.config(bg=GREEN if done else CARD2,
                          fg="white" if done else DIM)
        pg_prog_var.set(done_count)
        pg_prog_lbl.config(text=f"{done_count} / 13")
        _update_cal_summary()

    _cal_group_grid_ref[0] = _refresh_cal_group_grid

    def _refresh_cal_group_grid_outer():
        _refresh_cal_group_grid()

    # Override the outer reference
    def _refresh_cal_group_grid_real():
        _refresh_cal_group_grid()

    for g in range(1, 14):
        col = (g-1) % 7
        row = (g-1) // 7
        gb = tk.Button(pg_grid_frame, text=str(g), bg=CARD2, fg=DIM,
                        font=FSB, width=3, height=1, relief="flat", cursor="hand2",
                        borderwidth=0, highlightthickness=0,
                        padx=4, pady=4,
                        activebackground=BLUE_H, activeforeground="white")
        gb.grid(row=row, column=col, padx=2, pady=2)
        pg_group_btns[g] = gb
        gb.config(command=lambda _g=g: _select_pg_group(_g))

    def _select_pg_group(g):
        pg_listbox.selection_clear(0, tk.END)
        pg_listbox.selection_set(g - 1)
        pg_listbox.see(g - 1)
        _refresh_pergroup_ui()

    # Hidden listbox (drives the logic, UI hidden behind group grid)
    pg_listbox = tk.Listbox(f_pg, height=1, width=1, bg=CARD, fg=TEXT,
                             selectbackground=BLUE, font=FN, highlightthickness=0)
    for g in range(1, 14):
        pg_listbox.insert(tk.END, f"Group {g}  ({'5' if g == 1 else '10'} shades)")
    pg_listbox.selection_set(0)
    pg_listbox.place(x=-9999, y=-9999, width=1, height=1)

    # Selected group details
    pg_detail_frame = tk.Frame(f_pg, bg=CARD2, padx=10, pady=8)
    pg_detail_frame.pack(fill=tk.X, pady=(0, 8))

    pg_group_title_var = tk.StringVar(value="Group 1  (5 shades)")
    pg_group_title_lbl = lbl(pg_detail_frame, "", fg=TEXT, font=FB, bg=CARD2)
    pg_group_title_lbl.pack(anchor="w")
    pg_group_title_lbl.config(textvariable=pg_group_title_var)

    pg_main_var = tk.StringVar(value="Main tile:  not captured")
    pg_main_lbl = tk.Label(pg_detail_frame, textvariable=pg_main_var, bg=CARD2,
                            fg=DIM, font=FM, wraplength=CWRAP-30, justify=tk.LEFT)
    pg_main_lbl.pack(anchor="w", pady=(4, 6))

    pg_main_btn = mk_btn(pg_detail_frame, "Capture Main Tile", None,
                          bg=ACCENT, fg="white", hbg=ACCENT_H, pady=7)
    pg_main_btn.pack(anchor="w", pady=(0, 8))

    # Shade buttons — two rows
    pg_shades_frame = tk.Frame(pg_detail_frame, bg=CARD2)
    pg_shades_frame.pack(anchor="w", pady=(0, 4))
    pg_row1 = tk.Frame(pg_shades_frame, bg=CARD2)
    pg_row1.pack(anchor="w", pady=(0, 3))
    pg_row2 = tk.Frame(pg_shades_frame, bg=CARD2)
    pg_row2.pack(anchor="w")
    pg_shade_btns = []
    for i in range(5):
        b = mk_btn(pg_row1, f"S{i+1}", None, bg=CARD, fg=DIM, hbg=HOVER,
                   padx=10, pady=5, font=FSB)
        b.pack(side=tk.LEFT, padx=(0, 3))
        pg_shade_btns.append(b)
    for i in range(5, 10):
        b = mk_btn(pg_row2, f"S{i+1}", None, bg=CARD, fg=DIM, hbg=HOVER,
                   padx=10, pady=5, font=FSB)
        b.pack(side=tk.LEFT, padx=(0, 3))
        pg_shade_btns.append(b)

    pg_shades_status_var = tk.StringVar(value="Shades: 0/5 captured")
    pg_shades_status_lbl = lbl(pg_detail_frame, "", fg=DIM, font=FS, bg=CARD2)
    pg_shades_status_lbl.pack(anchor="w")
    pg_shades_status_lbl.config(textvariable=pg_shades_status_var)

    def _get_selected_group():
        sel = pg_listbox.curselection()
        return int(sel[0]) + 1 if sel else 1

    def _refresh_pergroup_ui():
        g = _get_selected_group()
        cal = S["cal"]
        while len(cal.main_colors) < g:
            cal.main_colors.append(_default_main_colors()[len(cal.main_colors)])
        mc = cal.main_colors[g-1] if g <= len(cal.main_colors) else None
        n_shades = 5 if g == 1 else 10
        pg_group_title_var.set(f"Group {g}  ({n_shades} shades)")
        if mc:
            pg_main_var.set(f"Main tile:  {mc.pos}  {_rgb_to_hex(mc.rgb) or ''}"
                            if mc.pos else "Main tile:  not captured")
            n_cap = sum(1 for s in (mc.shades[:n_shades] or []) if s.pos)
            pg_shades_status_var.set(f"Shades: {n_cap}/{n_shades} captured")
            pg_main_lbl.config(fg=GREEN if mc.pos else DIM)
        # Update shade button colors
        for i in range(10):
            if i < n_shades:
                pg_shade_btns[i].config(state="normal")
                if mc and i < len(mc.shades) and mc.shades[i].pos:
                    pg_shade_btns[i].config(bg=GREEN, fg="white")
                else:
                    pg_shade_btns[i].config(bg=CARD, fg=DIM)
            else:
                pg_shade_btns[i].config(state="disabled", bg=MUTED, fg=MUTED)
        if g == 1:
            pg_row2.pack_forget()
        else:
            pg_row2.pack(anchor="w")
        # Update group tile color
        if g in pg_group_btns:
            pg_group_btns[g].config(bg=BLUE_H, fg="white")
        for other_g, b in pg_group_btns.items():
            if other_g == g: continue
            cal_other = S["cal"]
            if other_g <= len(cal_other.main_colors):
                mc_o = cal_other.main_colors[other_g-1]
                need_o = 5 if other_g == 1 else 10
                done_o = mc_o.pos and len(mc_o.shades) >= need_o and all(
                    s.pos for s in mc_o.shades[:need_o])
                b.config(bg=GREEN if done_o else CARD2,
                          fg="white" if done_o else DIM)
            else:
                b.config(bg=CARD2, fg=DIM)

    def _on_cap_main():
        g = _get_selected_group()
        cal = S["cal"]
        while len(cal.main_colors) < g:
            cal.main_colors.append(_default_main_colors()[len(cal.main_colors)])
        def done(pos, rgb):
            cal.main_colors[g-1].pos = pos
            cal.main_colors[g-1].rgb = rgb
            pg_main_var.set(f"Main tile:  {pos}  {_rgb_to_hex(rgb)}")
            pg_main_lbl.config(fg=GREEN)
            _refresh_pergroup_ui()
            _refresh_cal_group_grid()
            refresh_cal_status()
            _auto_save_cal()
        _capture_point(root, f"Click the MAIN color tile for Group {g}", done)

    def _on_cap_shade(idx):
        g = _get_selected_group()
        cal = S["cal"]
        while len(cal.main_colors) < g:
            cal.main_colors.append(_default_main_colors()[len(cal.main_colors)])
        mc = cal.main_colors[g-1]
        while len(mc.shades) <= idx:
            mc.shades.append(ShadeButton(name=f"Shade {len(mc.shades)+1}", pos=None, rgb=None))
        def done(pos, rgb):
            mc.shades[idx].pos = pos
            mc.shades[idx].rgb = rgb
            _refresh_pergroup_ui()
            _refresh_cal_group_grid()
            refresh_cal_status()
            _auto_save_cal()
        _capture_point(root, f"Click shade {idx+1} for Group {g}", done)

    pg_main_btn.config(command=_on_cap_main)
    for i in range(10):
        pg_shade_btns[i].config(command=lambda ii=i: _on_cap_shade(ii))
    pg_listbox.bind("<<ListboxSelect>>", lambda e: _refresh_pergroup_ui())
    _refresh_pergroup_ui()

    # ── STEP 3: Tool buttons ────────────────────────────────────────────────────
    f_tools = _cal_card("Tool Buttons  (paint & bucket fill)", step="STEP 3", color=BLUE)
    lbl(f_tools, "Required for bucket-fill acceleration. Capture the paint brush "
        "and bucket fill tool icons from the game toolbar.",
        fg=DIM, font=FS, bg=CARD, wraplength=CWRAP).pack(anchor="w", pady=(0, 8))

    pt_var = tk.StringVar(value="Paint tool:   not captured")
    bk_var = tk.StringVar(value="Bucket tool:  not captured")
    pt_lbl = tk.Label(f_tools, textvariable=pt_var, bg=CARD, fg=DIM, font=FM,
                       wraplength=CWRAP, justify=tk.LEFT)
    pt_lbl.pack(anchor="w")
    bk_lbl = tk.Label(f_tools, textvariable=bk_var, bg=CARD, fg=DIM, font=FM,
                       wraplength=CWRAP, justify=tk.LEFT)
    bk_lbl.pack(anchor="w", pady=(2, 8))

    def _ap_tool(which, pos, rgb):
        if which == "paint":
            S["cal"].paint_tool_pos = pos
            pt_var.set(f"Paint tool:   {pos}")
            pt_lbl.config(fg=GREEN)
        else:
            S["cal"].bucket_tool_pos = pos
            bk_var.set(f"Bucket tool:  {pos}")
            bk_lbl.config(fg=GREEN)
        refresh_cal_status(); _auto_save_cal()

    tb_row = tk.Frame(f_tools, bg=CARD)
    tb_row.pack(anchor="w")
    mk_btn(tb_row, "Capture Paint Tool",
           lambda: _capture_point(root, "Click the PAINT TOOL icon in-game",
                                  lambda p, r: _ap_tool("paint", p, r)),
           bg=BLUE, fg=TEXT, hbg=BLUE_H, pady=7).pack(side=tk.LEFT, padx=(0, 8))
    mk_btn(tb_row, "Capture Bucket Tool",
           lambda: _capture_point(root, "Click the BUCKET FILL icon in-game",
                                  lambda p, r: _ap_tool("bucket", p, r)),
           bg=BLUE, fg=TEXT, hbg=BLUE_H, pady=7).pack(side=tk.LEFT)

    # ── Save / Load ─────────────────────────────────────────────────────────────
    f_io = _cal_card("Save  /  Load  /  Export", color=MUTED)
    lbl(f_io, f"Auto-saves to {CALIBRATION_FILE} after every capture.",
        fg=DIM, font=FS, bg=CARD, wraplength=CWRAP).pack(anchor="w", pady=(0, 8))
    cf_var = tk.StringVar(value=CALIBRATION_FILE)
    tk.Label(f_io, textvariable=cf_var, bg=CARD, fg=DIM, font=FM,
             wraplength=CWRAP).pack(anchor="w", pady=(0, 8))

    def save_cal():
        path = filedialog.asksaveasfilename(
            title="Save Calibration", initialfile=CALIBRATION_FILE,
            filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path: return
        try:
            with open(path, "w") as f: json.dump(S["cal"].to_dict(), f, indent=2)
            cf_var.set(f"Saved → {os.path.basename(path)}")
            messagebox.showinfo("Saved", f"Calibration saved:\n{path}")
        except Exception as e: messagebox.showerror("Save Failed", str(e))

    def load_cal():
        path = filedialog.askopenfilename(
            title="Load Calibration",
            filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path: return
        try:
            with open(path) as f: d = json.load(f)
            use_zip = d.get("back_button_pos") is not None and d.get("main_colors") is not None
            cal = CalibData.from_zip_config_dict(d) if use_zip else CalibData.from_dict(d)
            S["cal"] = cal
            _sync_cal_ui()
            refresh_cal_status()
            _refresh_cal_group_grid()
            _auto_save_cal()
            cf_var.set(f"Loaded → {os.path.basename(path)}")
            messagebox.showinfo("Loaded", "Calibration loaded and saved.")
        except Exception as e:
            messagebox.showerror("Load Failed", str(e))

    def clear_cal():
        if messagebox.askyesno("Clear Calibration",
                                "Clear all calibration data?\nThis cannot be undone."):
            S["cal"] = CalibData()
            _sync_cal_ui()
            refresh_cal_status()
            _refresh_cal_group_grid()

    def export_zip_cal():
        path = filedialog.asksaveasfilename(
            title="Export for Heartopia-Image-Painter",
            initialfile="config.json",
            filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path: return
        try:
            with open(path, "w") as f:
                json.dump(S["cal"].to_zip_config_dict(), f, indent=2)
            messagebox.showinfo("Exported",
                f"Exported in Heartopia-Image-Painter format:\n{path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    io_row = tk.Frame(f_io, bg=CARD)
    io_row.pack(anchor="w")
    io_row1 = tk.Frame(io_row, bg=CARD)
    io_row1.pack(anchor="w", pady=(0, 6))
    mk_btn(io_row1, "💾  Save", save_cal,
           bg=GREEN, fg="#001a10", hbg=GREEN_H, pady=7).pack(side=tk.LEFT, padx=(0, 6))
    mk_btn(io_row1, "📂  Load", load_cal,
           bg=BLUE, fg=TEXT, hbg=BLUE_H, pady=7).pack(side=tk.LEFT, padx=(0, 6))
    mk_btn(io_row1, "📤  Export", export_zip_cal,
           bg=CARD2, fg=DIM, hbg=HOVER, pady=7).pack(side=tk.LEFT, padx=(0, 6))
    io_row2 = tk.Frame(io_row, bg=CARD)
    io_row2.pack(anchor="w")
    mk_btn(io_row2, "🗑  Clear calibration", clear_cal,
           bg=RED, fg="white", hbg=RED_H, pady=7).pack(side=tk.LEFT)

    # Bottom padding
    tk.Frame(cal_inner, bg=SURF, height=20).pack()

    # ── Right panel: calibration status display ────────────────────────────────
    cal_rf_inner = card_frame(cal_rf, bg=SURF, px=16, py=14)
    cal_rf_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    section_head(cal_rf_inner, "Calibration Status").pack(anchor="w", pady=(0, 10))
    div(cal_rf_inner, c=EDGE).pack(fill=tk.X, pady=(0, 8))

    cal_txt = tk.Text(cal_rf_inner, bg=INSET, fg=TEXT, font=FM, relief="flat",
                       state="disabled", wrap="none", highlightthickness=1,
                       highlightbackground=EDGE, selectbackground=BLUE,
                       insertbackground=TEXT)
    cal_txt.pack(fill=tk.BOTH, expand=True)

    cal_txt_scroll = ttk.Scrollbar(cal_rf_inner, orient="vertical",
                                    command=cal_txt.yview)
    cal_txt.configure(yscrollcommand=cal_txt_scroll.set)
    cal_txt_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _refresh_cal_display():
        cal = S["cal"]
        lines = []
        def row(label, val, ok=None):
            m = "  ✓" if ok is True else ("  ✗" if ok is False else "   ")
            lines.append(f"{m}   {label:<26} {val}")
        row("Paint tool",
            str(cal.paint_tool_pos) if cal.paint_tool_pos else "(not captured)")
        row("Bucket tool",
            str(cal.bucket_tool_pos) if cal.bucket_tool_pos else "(not captured)")
        row("Shade panel open",
            str(cal.shade_panel_open_xy) if cal.shade_panel_open_xy else "(uses main tile)")
        row("Shade panel back",
            str(cal.shade_panel_back_xy) if cal.shade_panel_back_xy else "(not captured — required)")
        if cal.main_colors and len(cal.main_colors) >= 13:
            lines.append("")
            lines.append("   Per-group calibration  (13 color groups)")
            lines.append("   " + "─" * 48)
            for i, mc in enumerate(cal.main_colors[:13]):
                need = 5 if i == 0 else 10
                ok = (mc.pos and len(mc.shades) >= need
                      and all(s.pos for s in (mc.shades[:need] or [])))
                cap = sum(1 for s in (mc.shades[:need] or []) if s.pos)
                tick = "✓" if ok else "✗"
                lines.append(f"   {tick}  {mc.name:<12}  main {'✓' if mc.pos else '✗'}"
                              f"  shades {cap}/{need}")
        lines.append("")
        lines.append("   " + ("✅  CALIBRATION COMPLETE" if cal.is_complete
                               else "⚠   Incomplete — capture missing fields above"))
        cal_txt.config(state="normal")
        cal_txt.delete("1.0", "end")
        cal_txt.insert("end", "\n".join(lines))
        cal_txt.config(state="disabled")

    _refresh_cal_display()

    def _sync_cal_ui():
        cal = S["cal"]
        pt_var.set(f"Paint tool:   {cal.paint_tool_pos}"
                   if cal.paint_tool_pos else "Paint tool:   not captured")
        bk_var.set(f"Bucket tool:  {cal.bucket_tool_pos}"
                   if cal.bucket_tool_pos else "Bucket tool:  not captured")
        pt_lbl.config(fg=GREEN if cal.paint_tool_pos else DIM)
        bk_lbl.config(fg=GREEN if cal.bucket_tool_pos else DIM)
        sp_open_var.set(f"Shades panel open:  {cal.shade_panel_open_xy}"
                        if cal.shade_panel_open_xy else "Shades panel open:  (uses main tile)")
        sp_back_var.set(f"Shade panel back:   {cal.shade_panel_back_xy}"
                        if cal.shade_panel_back_xy else "Shade panel back:   not captured")
        sp_open_lbl.config(fg=GREEN if cal.shade_panel_open_xy else DIM)
        sp_back_lbl.config(fg=GREEN if cal.shade_panel_back_xy else DIM)
        try: _refresh_pergroup_ui()
        except Exception: pass
        _refresh_cal_display()

    def _do_refresh_cal_status():
        """Single unified cal status refresh — updates sidebar, cal display, summary, ready bar."""
        cal = S["cal"]
        # Sidebar step 4
        if cal.is_complete:
            bk = " + 🪣" if (cal.paint_tool_pos and cal.bucket_tool_pos) else ""
            per = " (per-group)" if (cal.main_colors and len(cal.main_colors) >= 13
                                     and cal.shade_panel_back_xy) else ""
            cal_info_var.set(f"Ready{per}{bk}")
            cal_badge_lbl.config(fg=GREEN)
            s4_var.set("✓"); s4_slbl.config(fg=GREEN)
        else:
            if cal.main_colors and len(cal.main_colors) >= 13:
                done = sum(1 for i, mc in enumerate(cal.main_colors[:13])
                           if mc.pos and len(mc.shades) >= (5 if i == 0 else 10)
                           and all(s.pos for s in mc.shades[:(5 if i == 0 else 10)]))
                cal_info_var.set(f"Per-group: {done}/13 groups")
            else:
                cal_info_var.set("Incomplete — go to Calibration tab")
            cal_badge_lbl.config(fg=AMBER); s4_var.set("⚠"); s4_slbl.config(fg=AMBER)
        _refresh_step_badges()
        try: _refresh_cal_group_grid()
        except Exception: pass
        # Cal display
        _refresh_cal_display()
        # Cal summary header
        _update_cal_summary()
        # Ready bar
        steps_done = (bool(S.get("img_path")), True,
                      bool(S.get("canvas_det")), cal.is_complete)
        if sum(steps_done) == 4:
            ready_var.set("✅  All steps complete — ready to paint!")
            ready_lbl.config(fg=GREEN)
        else:
            missing = []
            if not steps_done[0]: missing.append("image")
            if not steps_done[2]: missing.append("canvas")
            if not steps_done[3]: missing.append("calibration")
            ready_var.set(f"⚠  Still needed: {', '.join(missing)}")
            ready_lbl.config(fg=AMBER)

    # Auto-load calibration
    cal_path = Path(CALIBRATION_FILE)
    if cal_path.exists():
        try:
            with open(cal_path) as f: d = json.load(f)
            use_zip = (d.get("back_button_pos") is not None
                       and d.get("main_colors") is not None)
            cal_loaded = (CalibData.from_zip_config_dict(d) if use_zip
                          else CalibData.from_dict(d))
            if cal_loaded.is_complete or (len(cal_loaded.sub5) == 5 and len(cal_loaded.sub10) == 10):
                S["cal"] = cal_loaded
                _sync_cal_ui()
                refresh_cal_status()
                _refresh_cal_group_grid()
                cf_var.set(f"Auto-loaded: {CALIBRATION_FILE}")
        except Exception:
            pass

    _refresh_cal_group_grid()

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3 — OPTIONS
    # ══════════════════════════════════════════════════════════════════════════
    opt_scroll_canvas = tk.Canvas(t3, bg=SURF, highlightthickness=0)
    opt_scroll_bar = ttk.Scrollbar(t3, orient="vertical", command=opt_scroll_canvas.yview)
    opt_inner = tk.Frame(opt_scroll_canvas, bg=SURF)
    opt_inner.bind("<Configure>", lambda e: opt_scroll_canvas.configure(
        scrollregion=opt_scroll_canvas.bbox("all")))
    opt_win = opt_scroll_canvas.create_window((0, 0), window=opt_inner, anchor="nw")
    opt_scroll_canvas.configure(yscrollcommand=opt_scroll_bar.set)
    opt_scroll_bar.pack(side=tk.RIGHT, fill=tk.Y)
    opt_scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    opt_scroll_canvas.bind("<Configure>",
                            lambda e: opt_scroll_canvas.itemconfig(opt_win, width=e.width))
    opt_scroll_canvas.bind("<MouseWheel>",
                            lambda e: opt_scroll_canvas.yview_scroll(
                                int(-1*(e.delta/120)), "units"))

    OWRAP = 600

    def _opt_card(title, color=BLUE):
        f = tk.Frame(opt_inner, bg=CARD, padx=20, pady=14)
        f.pack(fill=tk.X, padx=16, pady=(10, 0))
        section_head(f, title, color=color).pack(anchor="w", pady=(0, 8))
        div(f, c=EDGE).pack(fill=tk.X, pady=(0, 10))
        return f

    def _toggle_row(p, text, var, desc=None):
        row = tk.Frame(p, bg=CARD, pady=3)
        row.pack(fill=tk.X)
        cb = tk.Checkbutton(row, variable=var, bg=CARD, fg=TEXT, font=FB,
                             selectcolor=BLUE, activebackground=CARD,
                             activeforeground=TEXT, relief="flat",
                             highlightthickness=0, cursor="hand2",
                             text=text)
        cb.pack(side=tk.LEFT)
        if desc:
            lbl(row, desc, fg=DIM, font=FS, bg=CARD).pack(side=tk.LEFT, padx=(4, 0))
        return cb

    def _slider_row(p, text, var, lo, hi, res=0.001, fmt="{:.3f}s"):
        row = tk.Frame(p, bg=CARD, pady=4)
        row.pack(fill=tk.X)
        lbl(row, text, fg=DIM, font=FN, bg=CARD, width=22, anchor="w").pack(side=tk.LEFT)
        val_lbl = lbl(row, "", fg=GREEN, font=FM, bg=CARD, width=8, anchor="e")
        val_lbl.pack(side=tk.RIGHT)
        sl = tk.Scale(row, variable=var, from_=lo, to=hi, resolution=res,
                       orient=tk.HORIZONTAL, bg=CARD, fg=TEXT, troughcolor=EDGE2,
                       highlightthickness=0, showvalue=False, length=180,
                       sliderlength=14, sliderrelief="flat",
                       activebackground=BLUE_H, cursor="hand2")
        sl.pack(side=tk.RIGHT, padx=8)
        def upd(*_): val_lbl.config(text=fmt.format(var.get()))
        var.trace_add("write", upd); upd()
        return sl

    # Paint mode section
    f_mode = _opt_card("Paint Mode")
    _toggle_row(f_mode, "Rapid-click strokes", rapid_click_v,
                "— click each cell in run (recommended ✓)")
    _toggle_row(f_mode, "Drag strokes", drag_v,
                "— true mouse drag (alternative)")
    _toggle_row(f_mode, "Double-paint", double_v,
                "— extra tap per cell (slower, high-reliability fallback)")

    # Bucket fill section
    f_bucket = _opt_card("Bucket Fill  (requires Paint + Bucket tool calibration)")
    _toggle_row(f_bucket, "Base bucket-fill", bucket_v,
                "— flood-fill canvas with most-used shade first")
    _toggle_row(f_bucket, "Region bucket-fill", region_v,
                "— outline large color regions then bucket-fill interior")

    # Verification section
    f_verify = _opt_card("Verification")
    _toggle_row(f_verify, "Streaming verify", stream_v,
                "— check painted cells while painting (catches misses early)")
    _toggle_row(f_verify, "Auto-recover", auto_recover_v,
                "— skip stuck verify loops and resync palette state")

    # Timing section
    f_timing = _opt_card("Click Timing", color=AMBER)
    lbl(f_timing, "Increase delays if colors are applied wrong or cells are missed.",
        fg=DIM, font=FS, bg=CARD, wraplength=OWRAP).pack(anchor="w", pady=(0, 10))
    _slider_row(f_timing, "Mouse hold per click:", hold_v, 0.001, 0.10)
    _slider_row(f_timing, "After-click delay:", after_v, 0.001, 0.15)
    _slider_row(f_timing, "Palette click delay:", pal_v, 0.02, 0.35)

    # Tips section
    f_tips = _opt_card("Tips & Troubleshooting", color=GREEN)
    tips = [
        ("Wrong colors applied", "→ Increase Palette delay to ≥ 0.15s"),
        ("Cells skipped/missed", "→ Enable Double-paint or increase After-click delay"),
        ("Game not registering", "→ Use Rapid-click strokes (most reliable)"),
        ("Bucket fill spills",   "→ Increase After-click delay; it will disable itself safely"),
        ("Painting is too slow", "→ Lower delays, use Rapid-click, enable Streaming verify"),
    ]
    for problem, solution in tips:
        tip_row = tk.Frame(f_tips, bg=CARD, pady=2)
        tip_row.pack(fill=tk.X)
        lbl(tip_row, f"  {problem}", fg=DIM, font=FS, bg=CARD, width=28,
            anchor="w").pack(side=tk.LEFT)
        lbl(tip_row, solution, fg=GREEN, font=FS, bg=CARD).pack(side=tk.LEFT)

    tk.Frame(opt_inner, bg=SURF, height=20).pack()

    # ── Final init ─────────────────────────────────────────────────────────────
    _refresh_step_badges()
    refresh_cal_status()
    set_sbar("Ready — complete the 4 setup steps in the sidebar, then click Start Painting")

    root.mainloop()


if __name__ == "__main__":
    launch_gui()
