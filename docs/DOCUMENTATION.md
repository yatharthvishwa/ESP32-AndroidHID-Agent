# Documentation

1. [How it works](#how-it-works)
2. [Firmware](#firmware)
3. [phone.py](#phonepy)
4. [Calibration](#calibration)
5. [The agent](#the-agent)

---

## How it works

The ESP32-S3 has native USB, so it can pretend to be a **relative mouse**, a **keyboard** and a **media-key (consumer control) device**. Android treats it like any other USB peripheral and shows a pointer.

A relative mouse can only say "move by N counts", never "go to pixel (x, y)". So the system works like this:

1. **Home**: slam the cursor into the top-left corner (60 moves of −127, −127). Now the position is known: (0, 0).
2. **Move**: convert the target pixel distance to mouse counts using a calibrated **gain** (pixels per count, separately for X and Y), then send small constant-pace steps (2 counts every 10 ms). Constant pace keeps Android's pointer acceleration predictable, which is what the calibration relies on. Moves go one axis at a time for the same reason.
3. **Act**: click, press-and-drag, type, etc.
4. The firmware tracks the cursor by dead reckoning and **re-homes every 5 actions** (and after every swipe) so errors don't accumulate.

The computer does everything else. The firmware is deliberately thin: it receives one text command per line over TCP, and replies `ok`, `ok <data>`, or `err <reason>`. Screen size and gains are **sent by the computer at connect time**, so you never reflash when you change phones.

## Firmware

File: `firmware/hid_interface/hid_interface.ino`

### Settings you edit

| Constant | Default | Purpose |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASS` | placeholders | Your **2.4 GHz** network (the ESP32-S3 has no 5 GHz) |
| `HOSTNAME` | `phonehid` | mDNS name, reachable as `phonehid.local` |
| `PORT` | `7300` | TCP command port |
| `BEACON_PORT` | `7301` | UDP broadcast port used for discovery |

### Settings you usually leave alone

| Constant | Default | Purpose |
|---|---|---|
| `STEP_COUNTS` / `STEP_DELAY_MS` | `2` / `10` | Movement pace. **Recalibrate if you change these** |
| `WDT_TIMEOUT_S` | `10` | Reboot if `loop()` stalls this long |
| `WIFI_DOWN_REBOOT_MS` | `180000` | Reboot if Wi-Fi stays down this long |
| `HELD_RELEASE_MS` | `15000` | Release a held mouse button after this long with no commands |

### Shrike-Fi board: which USB port to use

The Shrike-Fi has separate ports for programming and for acting as a USB device:

1. **Flash from the UART port.**
2. Unplug, then connect the board to the phone with the **power USB-C port**. **Do not use the UART port for the phone**; the HID will not work through it.

### Arduino IDE settings

- **USB Mode:** `USB-OTG (TinyUSB)`
- **USB CDC On Boot:** `Disabled`
- Works with Arduino-ESP32 core 2.x and 3.x (the watchdog and `server.accept()` calls have compatibility handling; on older cores replace `server.accept()` with `server.available()`).

### Reliability features

- **Task watchdog**: a hung firmware reboots itself. `hang` (test only) lets you watch it happen.
- **Wi-Fi supervision**: reconnect attempts every 15 s; full reboot after 3 min offline.
- **Safe HID state**: all buttons/keys are released on disconnect, Wi-Fi loss, or reboot.
- **Discovery beacon**: every 3 s the board broadcasts `phonehid <ip> <port>` on UDP 7301.
- **`status` command** reports the reset reason: `poweron` (just plugged in), `brownout` (power problem, try a better cable/port), `task_wdt` (firmware hung), etc.

## phone.py

The Python library that gives you `Phone`: ADB for eyes, the ESP32 for hands.

### Finding the board

Nothing needs configuring. It tries, in order:

1. An explicit host (`Phone(host=...)` or `PHONE_HOST`)
2. The last IP that worked (cached in `~/.phonehid_ip`)
3. `phonehid.local`
4. The board's UDP broadcast (listens 5 s)
5. A scan of the local /24 network on port 7300

> The board serves **one client at a time**, and probing counts as a connection. Running `phone.py` while the agent or calibrator is connected will kick the other program off.

### Connection behaviour

- On connect, `phone.py` sends `screen <w> <h>`, `gain <gx> <gy>` and `rehome <n>` to the board. Until the board has both screen and gain, it refuses `tap`/`dtap`/`swipe`/`move`.
- Commands are **never retried automatically**: a tap that may already have happened must not be sent twice. After a `PhoneError`, call `reconnect()` and decide whether to repeat.
- The screen size is read from `adb shell wm size` (the override line wins if one is set).

### API

| Method | Notes |
|---|---|
| `tap(x, y)`, `double_tap(x, y)` | Pixels in real screen coordinates, clamped to the screen |
| `swipe(x1, y1, x2, y2, step_delay_ms=10)` | Press, drag, release. Higher delay = slower swipe. Firmware clamps to 2 to 50 ms |
| `scroll(clicks)` | Mouse wheel |
| `type_text(text)` | ASCII only; newlines become spaces (use `key("enter")`) |
| `key(name)` | `enter esc back tab backspace space up down left right gui vol_up vol_down` |
| `home_button()`, `back_button()` | Sent through **ADB** keyevents (more reliable than HID shortcuts). Note `key("back")` is just Esc over HID |
| `screenshot()` | PNG bytes via `adb exec-out screencap -p` |
| `home_cursor()` | Re-home the pointer to (0, 0) |
| `status()`, `ping()`, `board_ip()`, `cursor_estimate()`, `reboot_board()` | Diagnostics |
| `connect()` / `reconnect()` / `close()` | Connection management |

### Files it writes in your home directory

| File | Contents |
|---|---|
| `~/.phonehid_ip` | Last working board IP |
| `~/.phonehid_gain` | Saved gains (`gx gy`); defaults to `3.81 3.81` if missing |

## Calibration

Every phone/Android version has slightly different pointer acceleration, so the pixels-per-count **gain** has to be measured once per phone.

### Running it

```bash
cd python
pip install opencv-python-headless   # only needed for the automatic mode; manual mode works without it
python android_hid_calibrator.py
```

Open <http://127.0.0.1:8765> (change with `CALIBRATOR_HOST` / `CALIBRATOR_PORT`) and click **Use manual fallback**. Keep the phone screen on and still.

> **Use manual calibration.** Automatic calibration is not reliable right now, so don't use the "Automatic calibration" button. The steps below describe manual mode; automatic mode is covered only as background.

The calibrator serves a small web UI from a `static/` folder next to the script (`index.html`, `app.js`, `style.css`). It has three buttons:

| Button | What it does |
|---|---|
| **Automatic calibration** | Tries to find the cursor itself. Not reliable right now; skip it |
| **Use manual fallback** | Starts manual calibration (or switches to manual mid-run, keeping points already recorded). **Use this one** |
| **Home cursor** | Slams the cursor to the top-left and refreshes the screenshot |

**Manual calibration, step by step**

1. Press **Use manual fallback**. The page homes the cursor, moves it a known distance and shows the screenshot.
2. Click the **centre of the mouse cursor** in the screenshot (the cursor itself, not an icon beneath it). The page maps your click to real phone pixels.
3. Repeat for all 8 points. The progress bar tracks them.
4. On the last point the gains are fitted, pushed to the board and saved. The page shows X and Y gain, and the message reports the fit error in pixels.

### What it does

- Takes **8 samples**: four X distances and four Y distances (18/32/46/60 % of the screen), each starting from a freshly homed cursor.
- For each sample it homes the cursor, moves it a known number of counts, and screenshots the result.
- **Manual mode (use this):** you **click the centre of the visible cursor** in the screenshot for each of the 8 points. Click as precisely as you can; this is the main source of accuracy.
- *Automatic mode (not reliable right now):* tries to find the cursor by image differencing (OpenCV) and falls back to manual clicks when it can't. Skip it. Manual mode doesn't need OpenCV at all.
- Fits a line (with intercept) per axis. The slope is the gain; the intercept absorbs the offset between the cursor glyph's visual centre and its true hotspot.
- **No taps or clicks are sent to the phone**, only mouse movement.
- Pushes the new gain to the board immediately and writes `calibration.json`:

```json
{
  "gain_x": 3.60417,
  "gain_y": 3.51757,
  "screen_width": 720,
  "screen_height": 1600,
  "rmse_x_px": 4.78,
  "rmse_y_px": 2.07,
  "saved_at": "2026-10-22 21:40:01"
}
```

(Example values from one phone; yours will differ.)

### Reading the result

`rmse_*_px` is how far the samples sit from the fitted line. A few pixels is good. Large values (tens of pixels) usually mean imprecise clicks or non-constant acceleration; re-run it and click the cursor centre more carefully.

### ⚠️ Apply the gains to `phone.py`

The calibrator saves `calibration.json` and updates the board for **that session**, but `phone.py` and the agent read their gains from `~/.phonehid_gain` (default `3.81`). After calibrating, run:

```bash
python phone.py gain <gain_x> <gain_y>
```

or have the calibrator do it automatically by adding this to `_finish()` in `android_hid_calibrator.py`, right after `self.gain_x, self.gain_y = gx, gy`:

```python
from phone import _write_gains
_write_gains(gx, gy)
```

Redo calibration whenever you change phone, Android version, mouse settings, or `STEP_COUNTS` / `STEP_DELAY_MS`.

## The agent

File: `python/agent_openrouter.py`

### Loop

1. Take a screenshot, downscale it (long edge ≤ 1280 px), optionally draw a grid, send it with the task.
2. The model must reply with **exactly one tool call**.
3. Coordinates the model gives are mapped from screenshot pixels back to real phone pixels.
4. The action runs, the agent waits 0.8 s for the UI to settle, takes a fresh screenshot, and repeats.
5. It ends when the model calls `done`, replies without a tool, or hits `MAX_STEPS`.

### Tools the model can call

`tap`, `double_tap`, `swipe`, `type_text`, `key`, `home`, `back`, `wait`, `done(success, summary)`.

### Design details

- **Only the newest screenshot is kept** in the conversation; older ones are replaced with a text placeholder to save tokens.
- **Fraction tolerance**: if every coordinate is between 0 and 1, it's treated as a fraction of the screen (weak models do this).
- **No replays**: if an action errors, the model is told it "may or may not have happened" and must check the screen. On connection errors the board connection is re-established.
- **Retries** (exponential backoff, 6 attempts) apply only to the model call (rate limits, timeouts, 5xx), never to phone actions. 400/401/402/404 errors are raised immediately (bad key, model id, credits, or no vision/tool support).

### Choosing a model

The model must support **image input** and **tool/function calling**. If it doesn't, you'll see a 400/404 from OpenRouter. Free (`:free`) models are rate-limited and some aim less accurately; try `GRID=1` with weaker ones. I use `dots-studio/dots-3-note-preview:free`, which has been reliable and consistently available.

### Tips for good tasks

- Be specific and finite: "Open Settings → About phone and tell me the Android version."
- Start with the phone unlocked on the home screen. The agent can't type a PIN (and shouldn't).
- Use `MAX_STEPS` to cap cost while experimenting.
