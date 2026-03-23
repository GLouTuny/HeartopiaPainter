# Heartopia Painter (GLouTuny)

Automated canvas painter for **Heartopia** with a full GUI, in-app calibration, bucket-fill, and verification.

---

## What you need first

### 1. Python

- **Python 3.10+** recommended (3.11 / 3.13 work on Windows).
- During install, enable **“Add Python to PATH”** (Windows).

### 2. Install dependencies

From the project folder (where `requirements.txt` lives):

```bash
pip install -r requirements.txt
```

This installs:

| Package     | Role |
|------------|------|
| `pyautogui` | Mouse movement and clicks |
| `Pillow`    | Image load, preview, screen grabs |
| `numpy`     | Stripe-based canvas auto-detect |
| `keyboard`  | **F10** pause/resume, **F12** stop (optional but recommended) |
| `pynput`    | Fallback drag strokes |
| `mss`       | Fast pixel reads for verify/streaming (strongly recommended) |

If `keyboard` fails to install on Windows, run the terminal **as Administrator** once, or use the GUI **Pause / Resume / Stop** buttons only.

### 3. Run the app

```bash
python gloutuny_painter.py
```

No extra folders are required; keep everything in this repo root unless you choose otherwise.

---

## Game setup: fullscreen & resolution (important)

The painter uses **screen pixel coordinates** for every click. Calibration and canvas detection assume the game looks **the same** as when you captured buttons and the grid.

**Do this before calibrating and before each painting session:**

1. **Fullscreen** (or the same windowed size you will always use).
2. **Maximum / native resolution** for that display (or the same resolution every time).
3. **Same UI scale** (Windows display scaling should match what you used when calibrating — e.g. 100% vs 125%).
4. **Do not move** the game window or change resolution between:
   - Calibration captures  
   - Canvas detect / manual drag  
   - **Start Painting**

If you change resolution, fullscreen, or scaling, **re-detect the canvas** and **re-check calibration** (or clicks will miss cells and colors can be wrong).

---

## First-time workflow (quick)

1. **Paint** tab → **Browse Image** → choose your reference image.
2. Set **Ratio** and **Detail** (grid size). If you change the grid after detecting the canvas, click **Start Painting** again (the app rebuilds cell positions).
3. **Canvas** → **Auto Detect** (striped unpainted canvas) or **Manual Drag** around the grid.
4. **Calibration** tab → follow the steps (back button, per-group colors, paint + bucket tools). Data auto-saves to `heartopia_calibration.json`.
5. On **Calibration**, skip the **three crossed-out (×) colors** (do not calibrate those):

   <img width="163" height="135" alt="Three crossed-out colors to skip in calibration" src="https://github.com/user-attachments/assets/3d54fc7e-56c1-403f-be64-7f5c7e507396" />

6. **Paint** tab → **Start Painting** → switch to the game during the countdown.
7. **F10** pause/resume · **F12** stop (if `keyboard` is installed).

---

## Git: what to ignore & line endings

- **`.gitignore`** — ignores caches, virtual envs, and **local** files (`heartopia_calibration.json`, exports, logs).  
- **`.gitattributes`** — normalizes line endings (LF for `*.py`) and marks binaries so Git doesn’t corrupt images.

Commit **code** and **small shared assets**; keep **personal calibration** and huge exports out of the repo unless you intend to share them.

---

## Troubleshooting

| Problem | Try |
|--------|-----|
| Wrong colors / missed cells | Increase **Palette** / **After-click** delay in **Options**. |
| Progress / verify errors | Ensure grid matches canvas: re-detect after changing ratio/detail. |
| `mss` not installed | `pip install mss` — verify is much slower / less accurate without it. |
| No hotkeys | Use on-screen **Pause / Resume / Stop**; or fix `keyboard` install (admin prompt). |

---

## License / credits

Use and modify for your own Heartopia painting workflow. Keep game ToS in mind when automating input.
