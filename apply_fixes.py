#!/usr/bin/env python3
"""
GLouTuny Painter  v4.2  ->  v4.2-fixed
Apply 3 accuracy fixes found by comparing against Heartopia-Image-Painter reference.

ROOT CAUSE SUMMARY
==================
All three bugs together cause whole vertical lines to be skipped, then bucket-fill
to leak across the entire canvas with the wrong colour.

BUG 1 (CRITICAL — primary cause of vertical gaps + bucket flooding)
  Location : paint_by_color(), outline_opts creation
  Problem  : outline_opts is built with enable_drag=True + rapid_click_strokes=True,
             so outline cells are painted via _rapid_click_stroke with only 0.01s
             between clicks.  Heartopia misses some of those fast clicks, leaving
             holes in the outline.  When the bucket-fill runs it leaks through
             those holes and floods the whole canvas.
  Fix      : enable_drag=False, rapid_click_strokes=False  →  every outline cell
             gets a full _tap() with 0.06 s settle, matching the reference.

BUG 2 (SECONDARY — verify/repair reliability)
  Location : _verify_and_repair_color_group(), repair tap loop
  Problem  : Repair taps for contiguous runs of misses still use _rapid_click_stroke
             (0.01 s/click) when rapid_click_strokes=True.  The reference always
             uses individual _tap() for repairs.
  Fix      : Always use individual _tap() in the repair loop; remove the
             _rapid_click_stroke branch entirely.

BUG 3 (IMPROVEMENT — outline verification sampling accuracy)
  Location : _verify_outline(), pixel sampling
  Problem  : Uses _grab_canvas_pixels() (bulk ImageGrab) which can return stale
             pixels if the game hasn't finished rendering rapid strokes.  The
             reference uses per-pixel mss reads.
  Fix      : Replace _grab_canvas_pixels(canvas_rect, screen_pts) with
             _sample_pixels_mss(screen_pts).

Usage
-----
    python apply_fixes.py  your_painter.py
    python apply_fixes.py  your_painter.py  painter_fixed.py
"""

import sys


# ── Patch strings ─────────────────────────────────────────────────────────────

BUG1_OLD = (
    '"enable_drag": True,\n'
    '                                               "rapid_click_strokes": True, "double_paint": False,'
)
BUG1_NEW = (
    '"enable_drag": False,\n'
    '                                               "rapid_click_strokes": False, "double_paint": False,'
)

BUG2_OLD = (
    '            if opts.rapid_click_strokes and len(pts) >= 2:\n'
    '                _rapid_click_stroke(pts, opts, should_stop)\n'
    '            else:\n'
    '                for cx, cy in pts:\n'
    '                    if should_stop and should_stop():\n'
    '                        return\n'
    '                    _tap(cx, cy, opts)'
)
BUG2_NEW = (
    '            for cx, cy in pts:\n'
    '                    if should_stop and should_stop():\n'
    '                        return\n'
    '                    _tap(cx, cy, opts)'
)

BUG3_OLD = (
    '        screen_pts = [c2s(px,py) for px,py in coords]\n'
    '        rgbs = _grab_canvas_pixels(canvas_rect, screen_pts)'
)
BUG3_NEW = (
    '        screen_pts = [c2s(px,py) for px,py in coords]\n'
    '        rgbs = _sample_pixels_mss(screen_pts)'
)

PATCHES = [
    (BUG1_OLD, BUG1_NEW, "Bug 1  outline_opts  enable_drag=False  (CRITICAL)"),
    (BUG2_OLD, BUG2_NEW, "Bug 2  repair loop   always _tap()"),
    (BUG3_OLD, BUG3_NEW, "Bug 3  outline verify  _sample_pixels_mss"),
]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "painter.py"
    default_dst = src.replace(".py", "_fixed.py") if src.endswith(".py") else src + "_fixed"
    dst = sys.argv[2] if len(sys.argv) > 2 else default_dst

    print(f"\nReading : {src}")
    with open(src, encoding="utf-8") as f:
        code = f.read()

    print()
    all_ok = True
    for old, new, label in PATCHES:
        if old not in code:
            print(f"  WARN  [{label}]")
            print(f"        Pattern not found — file may already be patched,")
            print(f"        or the indentation/version does not match v4.2.")
            all_ok = False
        else:
            code = code.replace(old, new, 1)
            print(f"  OK    [{label}]")

    print()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"Writing : {dst}")

    if all_ok:
        print("\n✅  All 3 fixes applied.  Use the _fixed.py file.")
    else:
        print("\n⚠   One or more patches were NOT applied (see WARN lines above).")
        print("    Check that you are using the original v4.2 source file.")


if __name__ == "__main__":
    main()
