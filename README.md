# ESP32-AndroidHID-Agent

Let an AI model use a **real Android phone**, with no app installed on the phone and no root.

An ESP32-S3 plugs into the phone's USB port and shows up as an ordinary **USB mouse + keyboard**. Your computer takes screenshots over **ADB**, sends them to a vision model, and tells the ESP32 (over Wi-Fi) where to tap, swipe and type.

This is an Android port of the `esp32-iphone-agent` idea.

```
                       Wi-Fi (TCP :7300, UDP beacon :7301)
  ┌──────────────────┐ ───────────────────────────────────►  ┌────────────────┐   USB (HID)   ┌─────────────┐
  │     Computer     │                                        │   ESP32-S3     │ ────────────► │   Android   │
  │  phone.py        │ ◄───────────────────────────────────   │ mouse+keyboard │               │    phone    │
  │  agent (LLM)     │            "ok" / "err ..."            └────────────────┘               └─────────────┘
  └────────┬─────────┘                                                                                │
           │                         ADB screenshots (adb exec-out screencap -p)                      │
           └──────────────────────────────────────────────────────────────────────────────────────────┘
```

| Role | What does it | How |
|---|---|---|
| **Eyes** | Computer | `adb exec-out screencap -p` |
| **Hands** | ESP32-S3 | USB HID mouse, keyboard, media keys |
| **Brain** | Any vision + tool-calling model on [OpenRouter](https://openrouter.ai) | `agent_openrouter.py` |

> ⚠️ **This controls a real device.** The agent can open apps, type, and tap anything on screen. Use a spare/test phone, stay nearby on first runs, and read [Safety](#safety).

## Hardware

- **An ESP32-S3 board with native USB.** I'm using the **Shrike-Fi** board, but any ESP32-S3 works as long as the USB port you plug into the phone is wired to the chip's native USB (a plain ESP32, or a UART-bridge-only connection, cannot act as a USB keyboard/mouse).
- A USB-C cable (or OTG adapter) to connect the board to the phone.
- An Android phone with **USB debugging** enabled and a mouse-capable USB host port (virtually all of them).
- A computer on the **same 2.4 GHz Wi-Fi network** as the board, with [ADB](https://developer.android.com/tools/releases/platform-tools) installed.

> The phone's USB port is occupied by the ESP32, so ADB normally runs over Wi-Fi (**Developer options → Wireless debugging**, then `adb pair` / `adb connect`). If you have a second path to the phone (e.g. a dock), USB ADB also works. `phone.py` just calls `adb`, so use whatever makes `adb devices` show your phone. If several devices are attached, pass `adb_serial` to `Phone(...)`.

## Quick start

### 1. Flash the firmware

1. Install the Arduino IDE and the **esp32 board package** (by Espressif).
2. Open `firmware/hid_interface/hid_interface.ino`.
3. Edit the settings at the top:
   ```cpp
   const char* WIFI_SSID = "YOUR_2.4GHZ_SSID";
   const char* WIFI_PASS = "YOUR_PASSWORD";
   ```
4. Tools menu settings (important):
   - **Board:** your ESP32-S3 board (or *ESP32S3 Dev Module*)
   - **USB Mode:** `USB-OTG (TinyUSB)`
   - **USB CDC On Boot:** `Disabled`
5. Upload.

> **Shrike-Fi board: two ports, two jobs.**
> - **Flash it from the UART port.**
> - **After flashing, unplug it and connect the board to the phone using the power USB-C port, not the UART port.** The HID (mouse/keyboard) will **not** work through the UART port.
>
> Other ESP32-S3 boards: flash however you normally do, then connect the port that carries native USB (often labelled USB or OTG) to the phone.

To confirm the board joined Wi-Fi, look for `phonehid` in your router's client list, or run `python phone.py find`.

### 2. Install the Python side

```bash
cd python
pip install openai pillow opencv-python-headless
adb devices          # your phone should be listed
```

### 3. Check the connection

```bash
python phone.py find       # prints the board's IP
python phone.py test       # screenshot, tap the screen centre, screenshot, report whether the screen changed
```

Leave the phone on an empty home-screen area before `test`.

### 4. Calibrate (do this once per phone)

```bash
python android_hid_calibrator.py
# open http://127.0.0.1:8765 and click "Use manual fallback"
```

**Use manual calibration.** Automatic calibration is not reliable right now, so skip the "Automatic calibration" button. Click **Use manual fallback** instead. For each of the 8 points the page shows a screenshot with the mouse cursor on it; click the **center of the cursor itself** (not the icon underneath it). The page only moves the pointer; it never taps the phone.

Then **apply the result to `phone.py`**:

```bash
python phone.py gain 3.60417 3.51757    # use YOUR gain_x / gain_y from calibration.json
```

See [Calibration](docs/DOCUMENTATION.md#calibration) for why this extra step is needed and what good numbers look like.

### 5. Run the agent

```bash
export OPENROUTER_API_KEY=sk-or-...
export OPENROUTER_MODEL=dots-studio/dots-3-note-preview:free
python agent_openrouter.py "Open Settings and tell me the Android version"
```

Windows PowerShell:

```powershell
$env:OPENROUTER_API_KEY="sk-or-..."
$env:OPENROUTER_MODEL="dots-studio/dots-3-note-preview:free"
python agent_openrouter.py "Open Settings and tell me the Android version"
```

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | – | Required |
| `OPENROUTER_MODEL` | – | Required. Must support images **and** tool calling. I use `dots-studio/dots-3-note-preview:free`, which has been reliable and available |
| `GRID` | off | `GRID=1` draws a labelled coordinate grid on screenshots, which helps weaker models aim |
| `MAX_STEPS` | `25` | Step limit per task |
| `PHONE_HOST` | auto | Skip discovery and use this board IP/hostname |

## Using `phone.py` on its own

You don't need an AI to use the hardware layer:

```bash
python phone.py tap 360 800
python phone.py swipe 360 1200 360 400
python phone.py type "hello world"
python phone.py key enter
python phone.py scroll -3
python phone.py home
python phone.py shot screen.png
python phone.py status
```

or from Python:

```python
from phone import Phone

p = Phone()                      # finds the board, reads screen size over ADB, pushes settings
p.tap(360, 800)
p.swipe(360, 1200, 360, 400)     # scroll a list down
p.type_text("hello")
p.key("enter")
open("screen.png", "wb").write(p.screenshot())
```

## Safety

- **No authentication.** The board accepts commands from anyone who can reach TCP port 7300 on your network, and it serves one client at a time. Use a trusted home network, never an open or shared one.
- The agent's system prompt tells the model not to enter passwords, payment details, or send messages/purchases unless the task says so. That is a **prompt, not a guarantee**. Models make mistakes. Don't leave banking, messaging or payment apps logged in on the test phone.
- Safety nets in the firmware: a held mouse button is released after 15 s with no commands (and on disconnect), the task watchdog reboots a hung board after 10 s, and the board reboots if Wi-Fi is down for 3 minutes.

## Limitations

- Typing is **ASCII only** (the board types as a US-layout keyboard).
- Pointer-based input: the "touch" is an Android mouse cursor, so apps that require true multi-touch (pinch-zoom, two-finger gestures) can't be driven.
- Accuracy depends on calibration and Android's pointer acceleration. Expect a few pixels of error, so tiny targets can be missed.
- Cursor position is tracked by dead reckoning and re-homed to the top-left corner every 5 actions (and after every swipe).

## Documentation

- [Full documentation](docs/DOCUMENTATION.md): how it works, calibration, agent internals
- [Command protocol](internals/PROTOCOL.md): talk to the board with `nc`
- [Troubleshooting](internals/TROUBLESHOOTING.md)

## Credits

Based on the `esp32-iphone-agent` project idea. Built on the Arduino-ESP32 USB HID libraries, ADB, OpenCV, and the OpenRouter API.
