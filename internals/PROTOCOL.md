# ESP32 command protocol

The firmware speaks a tiny line-based text protocol over **TCP port 7300**.

- One command per line, terminated by `\n` (a `\r` is ignored). Max line length is 200 characters.
- Every command gets one reply line:
  - `ok`
  - `ok <data>`
  - `err <reason>`
- **One client at a time.** A new connection replaces the old one.
- If the connection drops, any held mouse button/keys are released.

You can try it by hand:

```bash
nc phonehid.local 7300      # or the board's IP
screen 720 1600
gain 3.6 3.5
tap 360 800
```

## Configuration (must be sent after connecting)

`phone.py` sends these automatically. Until both `screen` and `gain` are set, `tap`, `dtap`, `swipe` and `move` return `err phone not configured; send screen and gain first`.

| Command | Description |
|---|---|
| `screen <w> <h>` | Screen size in pixels (200 to 20000 each). Resets the cursor and forces a re-home |
| `gain <gx> <gy>` | Pixels per mouse count for X and Y (0.1 to 100). Comes from calibration |
| `rehome <n>` | Re-home the cursor every *n* actions (1 to 100, default 5) |

## Pointer

| Command | Description |
|---|---|
| `tap <x> <y>` | Move to pixel and left-click |
| `dtap <x> <y>` | Double click |
| `swipe <x1> <y1> <x2> <y2> [delay_ms]` | Press at start, drag to end, release. `delay_ms` per 2-count step, clamped 2 to 50 (default 10) |
| `scroll <n>` | Mouse wheel, clamped to ±127 |
| `move <dx> <dy>` | Relative move in **raw mouse counts** (X first, then Y). Used by the calibrator |
| `press` / `release` / `click` | Left button down / up / click at the current position |
| `home` | Slam the cursor to the top-left and reset position tracking |
| `pos` | `ok <x> <y>`: estimated cursor position |

## Keyboard and media

| Command | Description |
|---|---|
| `type <text>` | Type the rest of the line as ASCII text |
| `key <name>` | Press one key: `enter`, `esc`, `back` (alias for esc), `tab`, `backspace`, `space`, `up`, `down`, `left`, `right`, `gui`, `vol_up`, `vol_down` |

Unknown key names return `err unknown key`.

## Diagnostics

| Command | Reply |
|---|---|
| `ping` | `ok` |
| `capabilities` | `ok firmware=2 hid=mouse,keyboard,consumer config=runtime screen=required gain=required` |
| `ip` | `ok <ip>` |
| `status` | `ok uptime=… reset=… rssi=… wifidrops=… heap=… configured=0/1 screen=WxH gain=gx,gy` |
| `reboot` | `ok`, then the board restarts |
| `hang` | **Testing only.** Freezes the firmware to demonstrate the watchdog reboot |

`reset=` is one of `poweron`, `software`, `panic`, `int_wdt`, `task_wdt`, `wdt`, `brownout`, `other`.

## Discovery beacon (UDP 7301)

Every 3 seconds the board broadcasts to the subnet's broadcast address:

```
phonehid <ip> <port>
```

`phone.py` listens for this when the `.local` name and cached IP don't work.
