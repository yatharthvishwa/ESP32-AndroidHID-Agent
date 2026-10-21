#!/usr/bin/env python3
"""Control an Android phone: screenshots through ADB, touch and keyboard
through the ESP32-S3 (phone_net.ino).

    python phone.py test
    python phone.py find                 # just locate the board and print its IP
    python phone.py tap 360 800
    python phone.py swipe 360 1200 360 400
    python phone.py type "hello"
    python phone.py key enter
    python phone.py shot screen.png
    python phone.py home

Finding the board (nothing to configure): an explicit host (argument or the
PHONE_HOST environment variable) is tried first, then the last IP that worked,
then the phonehid.local name, then the board's UDP broadcast, then a scan of
the local /24 network. The IP that works is remembered in ~/.phonehid_ip.
"""
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_NAME = "phonehid.local"
DEFAULT_PORT = 7300
BEACON_PORT = 7301
CACHE_PATH = os.path.expanduser("~/.phonehid_ip")


class PhoneError(Exception):
    pass


def _say(msg):
    print(f"[phone] {msg}", file=sys.stderr)


# ---------- finding the board ----------
def _probe(ip, port, timeout=1.0):
    """True if a phone_net board answers 'ping' with 'ok' at ip:port.
    Note: the board serves one client at a time, so probing it kicks off
    any other program that is connected."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"ping\n")
            return s.recv(16).strip() == b"ok"
    except OSError:
        return False


def _resolve(name):
    try:
        return socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0]
    except OSError:
        return None


def _read_cache(path):
    try:
        with open(path) as f:
            ip = f.read().strip()
        ipaddress.IPv4Address(ip)
        return ip
    except (OSError, ValueError):
        return None


def _write_cache(path, ip):
    try:
        with open(path, "w") as f:
            f.write(ip + "\n")
    except OSError:
        pass


def _listen_for_beacon(beacon_port, wait):
    """Wait up to `wait` seconds for the board's UDP broadcast. Returns
    (ip, port) or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", beacon_port))
        deadline = time.time() + wait
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            s.settimeout(left)
            try:
                data, addr = s.recvfrom(256)
            except socket.timeout:
                return None
            parts = data.decode("ascii", "ignore").split()
            if len(parts) == 3 and parts[0] == "phonehid":
                try:
                    ipaddress.IPv4Address(parts[1])
                    return parts[1], int(parts[2])
                except ValueError:
                    continue
    except OSError:
        return None
    finally:
        s.close()


def _local_prefix():
    """e.g. '192.168.31.' for the interface that reaches the network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))      # sends nothing; just picks a route
        return s.getsockname()[0].rsplit(".", 1)[0] + "."
    except OSError:
        return None
    finally:
        s.close()


def _scan(prefix, port):
    def check(i):
        ip = f"{prefix}{i}"
        try:
            with socket.create_connection((ip, port), timeout=0.4):
                pass
        except OSError:
            return None
        return ip if _probe(ip, port) else None

    with ThreadPoolExecutor(max_workers=64) as ex:
        for ip in ex.map(check, range(1, 255)):
            if ip:
                return ip
    return None


def find_board(host=None, port=DEFAULT_PORT, beacon_port=BEACON_PORT,
               cache_path=CACHE_PATH, beacon_wait=5.0, scan_prefix=None,
               scan=True):
    """Return the board's IP address, or raise PhoneError."""
    host = host or os.environ.get("PHONE_HOST")
    tried = []

    def good(ip, how):
        if _probe(ip, port):
            _write_cache(cache_path, ip)
            _say(f"board at {ip} (found by {how})")
            return ip
        tried.append(f"{how}: {ip} did not answer")
        return None

    if host and host != "auto":
        ip = _resolve(host)
        if ip is None:
            tried.append(f"name {host} did not resolve")
        else:
            found = good(ip, f"host {host}")
            if found:
                return found

    cached = _read_cache(cache_path)
    if cached:
        found = good(cached, "last known IP")
        if found:
            return found

    if not host or host == "auto":
        ip = _resolve(DEFAULT_NAME)
        if ip is None:
            tried.append(f"name {DEFAULT_NAME} did not resolve")
        else:
            found = good(ip, DEFAULT_NAME)
            if found:
                return found

    _say(f"listening {beacon_wait:.0f}s for the board's broadcast...")
    beacon = _listen_for_beacon(beacon_port, beacon_wait)
    if beacon:
        found = good(beacon[0], "broadcast")
        if found:
            return found
    else:
        tried.append("no broadcast heard")

    if scan:
        prefix = scan_prefix or _local_prefix()
        if prefix:
            _say(f"scanning {prefix}0/24 for port {port}...")
            ip = _scan(prefix, port)
            if ip:
                _write_cache(cache_path, ip)
                _say(f"board at {ip} (found by scan)")
                return ip
            tried.append(f"scan of {prefix}0/24 found nothing")

    raise PhoneError("cannot find the board. Tried: " + "; ".join(tried) +
                     ". Check the board's LED (red = no Wi-Fi) and that this "
                     "computer is on the same Wi-Fi network.")


class Phone:
    def __init__(self, host=None, port=DEFAULT_PORT, adb_serial=None,
                 gain_x=3.81, gain_y=3.81, screen=None, rehome_every=5,
                 timeout=30.0, beacon_port=BEACON_PORT, cache_path=CACHE_PATH,
                 beacon_wait=5.0, scan_prefix=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._beacon_port = beacon_port
        self._cache_path = cache_path
        self._beacon_wait = beacon_wait
        self._scan_prefix = scan_prefix
        self._adb = ["adb"] + (["-s", adb_serial] if adb_serial else [])
        self._sock = None
        self._file = None
        self.ip = None
        self._settings = (gain_x, gain_y, rehome_every)
        self.w, self.h = screen if screen else self.screen_size()
        self.connect()

    # ---------- board connection ----------
    def connect(self):
        """(Re)connect, finding the board again if its address changed, and
        push the per-phone settings so the firmware never needs reflashing."""
        self.close()
        self.ip = find_board(self.host, self.port, self._beacon_port,
                             self._cache_path, self._beacon_wait,
                             self._scan_prefix)
        try:
            self._sock = socket.create_connection((self.ip, self.port), timeout=10)
        except OSError as e:
            raise PhoneError(f"cannot reach board at {self.ip}:{self.port}: {e}")
        self._sock.settimeout(self.timeout)
        self._file = self._sock.makefile("rw", encoding="utf-8", newline="\n")
        gx, gy, rh = self._settings
        self._cmd(f"screen {self.w} {self.h}")
        self._cmd(f"gain {gx} {gy}")
        self._cmd(f"rehome {rh}")

    reconnect = connect

    def close(self):
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._file = self._sock = None

    def _cmd(self, line):
        """Send one command and return the data after 'ok' (may be empty).
        Never retries automatically: a tap that may already have happened
        must not be sent twice. After a PhoneError about the connection, call
        reconnect() and decide whether to repeat the action."""
        if "\n" in line or "\r" in line:
            raise ValueError("command must be a single line")
        try:
            self._file.write(line + "\n")
            self._file.flush()
            resp = self._file.readline()
        except (OSError, AttributeError) as e:
            self.close()
            raise PhoneError(f"lost connection to board during '{line.split()[0]}': {e}")
        if not resp:
            self.close()
            raise PhoneError("board closed the connection")
        resp = resp.strip()
        if resp == "ok":
            return ""
        if resp.startswith("ok "):
            return resp[3:]
        raise PhoneError(f"board said: {resp}")

    # ---------- eyes (ADB) ----------
    def screen_size(self):
        out = subprocess.check_output(self._adb + ["shell", "wm", "size"],
                                      timeout=10).decode()
        found = re.findall(r"(\d+)x(\d+)", out)
        if not found:
            raise PhoneError(f"could not read screen size from: {out!r}")
        return tuple(int(v) for v in found[-1])   # last line = override if any

    def screenshot(self):
        """PNG bytes of the current screen."""
        data = subprocess.check_output(self._adb + ["exec-out", "screencap", "-p"],
                                       timeout=20)
        if not data.startswith(b"\x89PNG"):
            raise PhoneError("screencap did not return a PNG (is ADB connected?)")
        return data

    def adb_key(self, code):
        subprocess.run(self._adb + ["shell", "input", "keyevent", str(code)],
                       check=True, timeout=10)

    # ---------- hands (ESP32) ----------
    def ping(self):
        self._cmd("ping")
        return True

    def status(self):
        """uptime, reset reason, Wi-Fi signal, Wi-Fi drops, free memory."""
        return self._cmd("status")

    def reboot_board(self):
        self._cmd("reboot")
        self.close()

    def _clamp(self, x, y):
        return (max(0, min(self.w - 1, int(round(x)))),
                max(0, min(self.h - 1, int(round(y)))))

    def tap(self, x, y):
        x, y = self._clamp(x, y)
        self._cmd(f"tap {x} {y}")

    def double_tap(self, x, y):
        x, y = self._clamp(x, y)
        self._cmd(f"dtap {x} {y}")

    def swipe(self, x1, y1, x2, y2, step_delay_ms=10):
        x1, y1 = self._clamp(x1, y1)
        x2, y2 = self._clamp(x2, y2)
        self._cmd(f"swipe {x1} {y1} {x2} {y2} {int(step_delay_ms)}")

    def scroll(self, clicks):
        """Mouse wheel. Negative scrolls content up, positive down."""
        self._cmd(f"scroll {int(clicks)}")

    def type_text(self, text):
        text = text.replace("\r", " ").replace("\n", " ")   # use key('enter') instead
        if not text.isascii():
            raise PhoneError("only ASCII text can be typed")
        self._cmd("type " + text)

    def key(self, name):
        """enter, esc, back, tab, backspace, space, up, down, left, right,
        gui, vol_up, vol_down"""
        self._cmd(f"key {name}")

    def home_cursor(self):
        self._cmd("home")

    def home_button(self):
        self.adb_key(3)      # via ADB: more reliable than a HID shortcut

    def back_button(self):
        self.adb_key(4)

    def cursor_estimate(self):
        x, y = self._cmd("pos").split()
        return int(x), int(y)

    def board_ip(self):
        return self._cmd("ip")


# ---------- self test / CLI ----------
def changed_fraction(png_a, png_b):
    """Fraction of pixels that differ clearly, or None if Pillow is missing."""
    try:
        import io
        from PIL import Image, ImageChops
    except ImportError:
        return None
    a = Image.open(io.BytesIO(png_a)).convert("L")
    b = Image.open(io.BytesIO(png_b)).convert("L")
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    return sum(hist[30:]) / float(a.size[0] * a.size[1])


def self_test():
    p = Phone()
    print(f"screen: {p.w}x{p.h}")
    print("ping:", p.ping(), "| board ip:", p.board_ip())
    print("status:", p.status())
    print("Taking screenshot, tapping the CENTER of the screen, taking another.")
    print("Leave the phone on an empty home-screen area first.")
    a = p.screenshot()
    p.tap(p.w // 2, p.h // 2)
    time.sleep(1.0)
    b = p.screenshot()
    frac = changed_fraction(a, b)
    if frac is None:
        print("install Pillow (pip install pillow) for an automatic screen-change check")
        print("screens identical:", a == b)
    else:
        print(f"changed pixels: {frac * 100:.2f}% "
              f"({'screen changed' if frac > 0.005 else 'no visible change'})")


def main(argv):
    if len(argv) < 2 or argv[1] == "test":
        return self_test()
    cmd, args = argv[1], argv[2:]
    if cmd == "find":
        print(find_board())
        return
    p = Phone()
    if cmd == "tap":
        p.tap(float(args[0]), float(args[1]))
    elif cmd == "swipe":
        p.swipe(*[float(v) for v in args[:4]])
    elif cmd == "type":
        p.type_text(" ".join(args))
    elif cmd == "key":
        p.key(args[0])
    elif cmd == "scroll":
        p.scroll(int(args[0]))
    elif cmd == "status":
        print(p.status())
        return
    elif cmd == "home":
        p.home_button()
    elif cmd == "shot":
        with open(args[0], "wb") as f:
            f.write(p.screenshot())
        print("saved", args[0])
    else:
        print(__doc__)
        return 1
    print("ok")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except PhoneError as e:
        print("error:", e)
        sys.exit(1)
