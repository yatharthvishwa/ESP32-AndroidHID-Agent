#!/usr/bin/env python3
"""Local browser calibration UI 

Uses OpenCV for screenshot-difference cursor detection. Manual click fallback remains available.
No taps/clicks are sent to Android during calibration; only mouse movement is sent.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from phone import Phone, PhoneError

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE / "calibration.json"
STATIC_DIR = HERE / "static"
HOST = os.environ.get("CALIBRATOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALIBRATOR_PORT", "8765"))
DEFAULT_GAIN = 3.81


def load_gains():
    try:
        data = json.loads(CALIBRATION_FILE.read_text())
        gx, gy = float(data["gain_x"]), float(data["gain_y"])
        if gx > 0 and gy > 0:
            return gx, gy
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return DEFAULT_GAIN, DEFAULT_GAIN


class Calibrator:
    def __init__(self):
        gx, gy = load_gains()
        self.phone = Phone(gain_x=gx, gain_y=gy)
        self.lock = threading.RLock()
        self.active = False
        self.points = []
        self.index = 0
        self.message = "Connected. Press Start calibration."
        self.error = None
        self.gain_x, self.gain_y = gx, gy
        self.last_frame = b""
        self.plan = []
        self.baseline_frame = b""
        self.auto_mode = False
        self.detector_message = ""

    def _make_plan(self):
        # Each point is reached from home so movement doesn't accumulate between samples.
        # Keep requested points well inside the screen to avoid edge clipping.
        gx = max(self.gain_x, 0.1)
        gy = max(self.gain_y, 0.1)
        plan = []
        for frac in (0.18, 0.32, 0.46, 0.60):
            plan.append({"axis": "x", "counts_x": max(1, round(self.phone.w * frac / gx)), "counts_y": 0})
        for frac in (0.18, 0.32, 0.46, 0.60):
            plan.append({"axis": "y", "counts_x": 0, "counts_y": max(1, round(self.phone.h * frac / gy))})
        return plan

    def _prepare_point(self):
        """Home, capture the old cursor, then move and capture the new cursor."""
        p = self.plan[self.index]
        self.phone.home_cursor()
        time.sleep(0.18)
        self.baseline_frame = self.phone.screenshot()
        self.phone._cmd(f"move {p['counts_x']} {p['counts_y']}")
        # Cursor may vanish while idle, so capture immediately after movement.
        time.sleep(0.10)
        self.last_frame = self.phone.screenshot()
        self.message = (f"Point {self.index + 1} of {len(self.plan)}: "
                        f"{p['axis'].upper()} movement; trying automatic cursor detection…")

    def _detect_cursor(self, before_png, after_png, axis):
        """Detect the moved cursor by differencing screenshots before/after HID movement.

        The old cursor and new cursor both change in the diff. For a positive X/Y
        movement, choose the plausible cursor-shaped component furthest along that axis.
        Returns the detected cursor center in screenshot pixels, or None.
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.detector_message = "Automatic detection requires OpenCV: pip install opencv-python-headless"
            return None
        import io
        try:
            before = cv2.imdecode(np.frombuffer(before_png, dtype=np.uint8), cv2.IMREAD_COLOR)
            after = cv2.imdecode(np.frombuffer(after_png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if before is None or after is None or before.shape != after.shape:
                self.detector_message = "Could not decode matching before/after screenshots."
                return None
            # Blur slightly to suppress compression/noise, then isolate changed pixels.
            a = cv2.GaussianBlur(before, (3, 3), 0)
            b = cv2.GaussianBlur(after, (3, 3), 0)
            diff = cv2.absdiff(a, b)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            _, mask = cv2.threshold(gray, 38, 255, cv2.THRESH_BINARY)
            kernel = np.ones((3, 3), np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            candidates = []
            h, w = gray.shape[:2]
            for contour in contours:
                x, y, cw, ch = cv2.boundingRect(contour)
                area = cv2.contourArea(contour)
                # Android cursor glyphs are small. Reject UI changes and broad animation regions.
                if not (12 <= area <= 1800 and 5 <= cw <= 64 and 7 <= ch <= 72):
                    continue
                ratio = cw / max(ch, 1)
                if not (0.18 <= ratio <= 2.2):
                    continue
                cx, cy = x + cw / 2.0, y + ch / 2.0
                candidates.append((cx, cy, area, cw, ch))
            if not candidates:
                self.detector_message = "No cursor-shaped changed region found; use manual fallback."
                return None
            # Prefer the moved cursor rather than the old one near the home corner.
            if axis == "x":
                directional = [c for c in candidates if c[0] > w * 0.04]
                pool = directional or candidates
                chosen = max(pool, key=lambda c: (c[0], c[2]))
            else:
                directional = [c for c in candidates if c[1] > h * 0.04]
                pool = directional or candidates
                chosen = max(pool, key=lambda c: (c[1], c[2]))
            self.detector_message = f"Detected cursor near ({chosen[0]:.0f}, {chosen[1]:.0f})."
            return chosen[0], chosen[1]
        except Exception as exc:
            self.detector_message = f"Cursor detection error: {exc}"
            return None

    def _record_point(self, x, y):
        p = self.plan[self.index]
        self.points.append({"axis": p["axis"], "counts_x": p["counts_x"],
                            "counts_y": p["counts_y"], "pixel_x": float(x), "pixel_y": float(y)})
        self.index += 1

    def _auto_worker(self):
        try:
            while self.active and self.index < len(self.plan):
                self._prepare_point()
                p = self.plan[self.index]
                detected = self._detect_cursor(self.baseline_frame, self.last_frame, p["axis"])
                if detected is None:
                    self.auto_mode = False
                    self.message = (f"Automatic detection paused at point {self.index + 1}/{len(self.plan)}. "
                                    f"{self.detector_message} Click the cursor in the screenshot to continue manually.")
                    return
                self._record_point(*detected)
                self.message = (f"Auto-detected point {self.index}/{len(self.plan)}. {self.detector_message}")
                # Give the browser time to observe progress and avoid hammering ADB.
                time.sleep(0.12)
            if self.active and self.index >= len(self.plan):
                self._finish()
        except Exception as exc:
            self.active = False
            self.error = str(exc)
            self.message = "Automatic calibration stopped. Check the connection and retry."

    def start(self, automatic=True):
        with self.lock:
            if self.active:
                raise ValueError("Calibration is already running.")
            self.points = []
            self.index = 0
            self.plan = self._make_plan()
            self.active = True
            self.auto_mode = bool(automatic)
            self.error = None
            self.detector_message = ""
            if self.auto_mode:
                self.message = "Automatic calibration started. Keep the phone screen still."
                threading.Thread(target=self._auto_worker, daemon=True).start()
            else:
                self._prepare_point()
                self.message = (f"Point 1 of {len(self.plan)}: click the CENTER of the visible mouse cursor. "
                                f"Axis {self.plan[0]['axis'].upper()}.")
            return self.status()

    def manual_fallback(self):
        with self.lock:
            if self.active:
                # Preserve successfully detected points and let the user identify
                # the current point instead of restarting the whole calibration.
                self.auto_mode = False
                self.message = (f"Manual fallback at point {self.index + 1}/{len(self.plan)}. "
                                "Click the center of the visible cursor in the screenshot.")
                return self.status()
            return self.start(automatic=False)

    def status(self):
        return {"active": self.active, "message": self.message, "error": self.error,
                "index": self.index, "total": len(self.plan), "width": self.phone.w,
                "height": self.phone.h, "gain_x": self.gain_x, "gain_y": self.gain_y,
                "points": self.points, "auto_mode": self.auto_mode, "detector_message": self.detector_message}

    def frame(self):
        with self.lock:
            if not self.last_frame:
                self.last_frame = self.phone.screenshot()
            return self.last_frame

    def add_click(self, x, y):
        with self.lock:
            if not self.active:
                raise ValueError("Start calibration first.")
            if not (0 <= x < self.phone.w and 0 <= y < self.phone.h):
                raise ValueError("Click must be inside the screenshot.")
            self._record_point(x, y)
            if self.index < len(self.plan):
                self._prepare_point()
                self.message = (f"Point {self.index + 1} of {len(self.plan)}: click the CENTER of the visible cursor. "
                                f"Axis {self.plan[self.index]['axis'].upper()}.")
                return self.status()
            self._finish()
            return self.status()

    def _fit_gain(self, axis):
        count_key = "counts_x" if axis == "x" else "counts_y"
        pixel_key = "pixel_x" if axis == "x" else "pixel_y"
        samples = [p for p in self.points if p["axis"] == axis and p[count_key] > 0]
        if len(samples) < 3:
            raise ValueError(f"Not enough {axis.upper()} samples; please retry calibration.")
        # Fit a line with an intercept. Cursor detection measures the glyph center,
        # not its Android hotspot, so the intercept absorbs that fixed visual offset.
        xs = [float(p[count_key]) for p in samples]
        ys = [float(p[pixel_key]) for p in samples]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom <= 0:
            raise ValueError(f"Not enough distinct {axis.upper()} movement distances; retry calibration.")
        gain = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
        intercept = mean_y - gain * mean_x
        if not (0.05 < gain < 100):
            raise ValueError(f"Calculated {axis.upper()} gain ({gain:.3f}) looks implausible; retry.")
        residuals = [y - (gain * x + intercept) for x, y in zip(xs, ys)]
        rmse = (sum(r * r for r in residuals) / len(residuals)) ** 0.5
        return gain, rmse

    def _finish(self):
        try:
            gx, err_x = self._fit_gain("x")
            gy, err_y = self._fit_gain("y")
            # Large residuals commonly mean the cursor was clicked inaccurately or
            # Android pointer acceleration varies. Keep the result but warn the user.
            self.gain_x, self.gain_y = gx, gy
            self.phone._settings = (gx, gy, self.phone._settings[2])
            self.phone._cmd(f"gain {gx:.5f} {gy:.5f}")
            self.phone.home_cursor()
            CALIBRATION_FILE.write_text(json.dumps({
                "gain_x": round(gx, 5), "gain_y": round(gy, 5),
                "screen_width": self.phone.w, "screen_height": self.phone.h,
                "rmse_x_px": round(err_x, 2), "rmse_y_px": round(err_y, 2),
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }, indent=2) + "\n")
            self.active = False
            self.message = (f"Calibration saved. X gain={gx:.3f}, Y gain={gy:.3f}. "
                            f"Fit residual: X {err_x:.1f}px, Y {err_y:.1f}px. "
                            "Test taps carefully; high residuals mean the result may be approximate.")
        except Exception as exc:
            self.active = False
            self.error = str(exc)
            self.message = "Calibration did not finish successfully. Press Start calibration to retry."


app = Calibrator()


class Handler(BaseHTTPRequestHandler):
    server_version = "AndroidHIDCalibrator/1.0"

    def log_message(self, fmt, *args):
        print("[calibrator] " + fmt % args)

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/app.js":
            self.send_bytes(200, (STATIC_DIR / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/style.css":
            self.send_bytes(200, (STATIC_DIR / "style.css").read_bytes(), "text/css; charset=utf-8")
        elif path == "/api/status":
            self.send_json(app.status())
        elif path == "/api/frame.png":
            try:
                self.send_bytes(200, app.frame(), "image/png")
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
        elif path == "/api/health":
            self.send_json({"ok": True, "board_ip": app.phone.ip, "screen": [app.phone.w, app.phone.h]})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/start":
                result = app.start(automatic=True)
            elif path == "/api/start-manual":
                result = app.manual_fallback()
            elif path == "/api/click":
                result = app.add_click(float(body["x"]), float(body["y"]))
            elif path == "/api/home":
                with app.lock:
                    app.phone.home_cursor()
                    app.last_frame = app.phone.screenshot()
                    app.message = "Cursor homed. Start calibration when ready."
                    result = app.status()
            else:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(result)
        except (ValueError, KeyError, PhoneError, OSError) as exc:
            self.send_json({"error": str(exc), **app.status()}, 400)
        except Exception as exc:
            self.send_json({"error": f"Unexpected error: {exc}", **app.status()}, 500)


def main():
    print(f"Android HID Calibrator: http://{HOST}:{PORT}")
    print(f"Board: {app.phone.ip} | Android screen: {app.phone.w}x{app.phone.h}")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopping calibrator...")
    finally:
        app.phone.close()


if __name__ == "__main__":
    try:
        main()
    except PhoneError as exc:
        raise SystemExit(f"Could not connect to the ESP32/ADB: {exc}")
