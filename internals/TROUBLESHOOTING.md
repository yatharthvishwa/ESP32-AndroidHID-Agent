# Troubleshooting

## Board can't be found ("cannot find the board")

- Check your router's client list for `phonehid`; if it isn't there, check the SSID/password in the firmware and that the network is **2.4 GHz**.
- Computer and board must be on the **same network/subnet**. Guest networks and "client isolation" block discovery.
- Try the IP directly: `PHONE_HOST=192.168.x.x python phone.py test`.
- `.local` names often fail on Windows or some routers. The UDP beacon and subnet scan are the fallback; give discovery a few seconds.
- Another program may be connected. The board serves one client at a time, so close the calibrator/agent first.

## Phone doesn't show a cursor, or nothing responds

- **Shrike-Fi:** flash from the **UART port**, then connect the **power USB-C port** to the phone. The HID will **not** work if the phone is plugged into the UART port.
- Other boards: use the port that carries native USB (often labelled USB or OTG), not the UART/serial bridge port.
- Re-check Arduino settings: **USB Mode: USB-OTG (TinyUSB)**, **USB CDC On Boot: Disabled**.
- Some phones need an OTG adapter or a data-capable cable.
- Move the mouse, e.g. `python phone.py swipe 100 100 500 500`, and watch for a pointer.
- Some Android versions/settings hide the pointer until it moves.

## Taps land in the wrong place

1. Calibrate (`android_hid_calibrator.py`).
2. **Apply the gains** with `python phone.py gain <x> <y>`. The calibrator does not update `~/.phonehid_gain` by itself (see [Calibration](DOCUMENTATION.md#calibration)).
3. Check `python phone.py status`: `gain=` should match your calibration, and `screen=` should match your phone.
4. Changed `STEP_COUNTS`/`STEP_DELAY_MS`, Android version, or pointer-speed setting? Recalibrate.
5. Errors that grow down/right across a sequence: lower the re-home interval (`rehome 3`) or recalibrate.

## `err phone not configured`

The board has restarted and forgot its screen size/gain. Reconnect with `phone.py` (it sends them on connect), or send `screen` and `gain` manually.

## ADB problems

- `adb devices` must list your phone as `device` (not `unauthorized` or `offline`).
- Since the ESP32 occupies the phone's USB port, use **Wireless debugging** (`adb pair`, then `adb connect <ip>:<port>`). The port changes when wireless debugging restarts.
- "screencap did not return a PNG": ADB isn't connected, or several devices are attached (pass `adb_serial`).
- Some banking/secure apps block screenshots and return a black screen.

## Board keeps rebooting / dropping out

Run `python phone.py status` and read `reset=`:

| reset | Likely cause |
|---|---|
| `brownout` | Power. Try a different cable/port/adapter |
| `task_wdt` | Firmware hung (please open an issue with what you were doing) |
| `poweron` | You just plugged it in |

Also check `rssi=` (weaker than about −80 dBm is flaky) and `wifidrops=`.

## Calibration problems

- Use **manual** calibration (click **Use manual fallback**). Automatic calibration is not reliable right now. If you do try it and it pauses with "No cursor-shaped changed region found", just continue by clicking the cursor.
- Blank page or errors in the browser: the `static/` folder (`index.html`, `app.js`, `style.css`) must sit next to `android_hid_calibrator.py`.
- Clicking the screenshot does nothing: a calibration must be running first (press **Use manual fallback**).
- High `rmse_*_px`: re-run and click the centre of the cursor more carefully.

## Agent problems

| Symptom | Fix |
|---|---|
| Exits: "Set OPENROUTER_API_KEY and OPENROUTER_MODEL" | Set both in the **same terminal** you run the script from |
| 400 / 404 from OpenRouter | Model doesn't exist or lacks **vision** or **tool calling** support |
| 401 / 402 | Bad API key / no credits |
| Repeated retries, 429 | Free models are rate-limited; wait, or use a paid model |
| Model taps near but not on the target | Use `GRID=1`; make sure calibration was applied |
| "model replied without calling a tool" | The model ignored the tool-calling instruction; use a better model |
| Agent loops without progress | Lower `MAX_STEPS` while testing; give a more specific task |
| Typing fails | Only ASCII is supported |
