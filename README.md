# Heartopia Painter — GLouTuny v4.1

Automated canvas painter for [Heartopia](https://www.heartopia.com/) with a desktop GUI: load an image, calibrate once, and paint the in-game canvas automatically. Includes in-app calibration, bucket-fill acceleration, and streaming verification.

## Features

- **Full GUI** — Paint, Calibration, and Options tabs
- **In-app calibration wizard** — Capture back button, shade panel, 13 color groups, paint/bucket tools
- **Fast pixel sampling** — Uses `mss` for per-pixel reads (5–10× faster than ImageGrab where used)
- **Bucket fill** — Base flood-fill and region outline + fill for large areas
- **Streaming verify** — Checks painted cells while painting; per-color post-pass verify
- **Pause / Stop** — F10 Pause, F12 Stop; interruptible sleeps

## Requirements

- **Python 3.10+** (tested on 3.10–3.13)
- **Windows** recommended (DPI awareness and hotkeys tuned for Windows; may work on macOS/Linux with minor tweaks)

## Install

1. Clone or download this repo and open a terminal in the project folder.

2. (Recommended) Create and use a virtual environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

   On macOS/Linux:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

   Or install manually:

   ```bash
   pip install pyautogui Pillow numpy keyboard pynput mss
   ```

## Run

From the **project root** (with the same environment activated as above):

```bash
python -m heartopia_painter
```

Alternatively, run the main module file directly:

```bash
python heartopia_painter/gloutuny_painter.py
```

The main window opens with three tabs:

1. **Paint** — Choose image, grid (ratio + detail), detect canvas, then **Start Painting**.
2. **Calibration** — Follow Step 1 (shade panel back/open), Step 2 (per-group main + shades), Step 3 (paint & bucket tools). Calibration auto-saves to `heartopia_calibration.json`.
3. **Options** — Paint mode, bucket/region fill, verification, and timing (hold, after-click, palette delay).

**Hotkeys (while painting):**

- **F10** — Pause / Resume
- **F12** — Stop

## Repository structure

Layout that other developers can follow:

```
HeartopiaPainter/
├── .gitattributes      # Line endings and binary handling (see below)
├── .gitignore
├── README.md
├── requirements.txt
├── heartopia_painter/           # Main application package
│   ├── __init__.py              # Package root; exports version and launch_gui
│   ├── __main__.py              # Entry point for: python -m heartopia_painter
│   └── gloutuny_painter.py      # Full app: GUI, paint engine, calibration, overlays
├── heartopia_canvas_detect.py   # Standalone canvas detection utilities (optional)
├── heartopia_v2/                # Older painter version (optional)
│   ├── heartopia_canvas_detect.py
│   ├── painter_v2.py
├── color.svg
├── color_loader.js
└── index_ref.html
```

Generated at runtime (not committed):

- `heartopia_calibration.json` — Your calibration data (backed by .gitignore).
- `heartopia_painted.json` — Optional painted-state cache.


## Project layout (reference)

| Path | Purpose |
|------|--------|
| `heartopia_painter/` | Main package. Run with `python -m heartopia_painter`. |
| `heartopia_painter/gloutuny_painter.py` | Single-file app: constants, palette, calibration types, paint engine, GUI, capture overlay. |
| `heartopia_canvas_detect.py` | Standalone canvas detection (optional). |
| `heartopia_v2/painter_v2.py` | Older painter version (optional). |
| `requirements.txt` | Python dependencies. |
| `heartopia_calibration.json` | Created after calibration; stored in project root (gitignored). |

## Calibration

- Run the app, go to **Calibration**, and complete **Step 1** (Back button, then optional Shades-Panel Open), **Step 2** (for each group 1–13: main tile + shade buttons S1–S5 or S1–S10), and **Step 3** (paint and bucket tool positions).
- You can **Export** calibration in the format used by Heartopia-Image-Painter and **Save** / **Load** JSON for backup.

## Tips

- Use **Rapid-click strokes** (Options) for best reliability.
- If colors are wrong, increase **Palette click delay** (e.g. ≥ 0.15s).
- If cells are skipped, enable **Double-paint** or increase **After-click delay**.
- Run the app as administrator only if needed for your environment (e.g. some games/overlays).

## License

Use and modify as you like. No formal license file in this repo.
