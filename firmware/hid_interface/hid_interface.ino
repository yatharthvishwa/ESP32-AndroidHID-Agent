// phone_net.ino  (with watchdog)
// ESP32-S3 USB keyboard + mouse for an Android phone, controlled over Wi-Fi.
// The computer sends one text command per line over TCP; the board replies
// "ok" or "ok <data>" or "err <reason>". All logic lives on the computer.
//
// Board options: keep the SAME board and USB settings you used for the HID
// sketch that worked (USB Mode: USB-OTG (TinyUSB), USB CDC On Boot: Disabled).

#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <esp_task_wdt.h>
#include <math.h>
#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDMouse.h"
#include "USBHIDConsumerControl.h"

USBHIDKeyboard Keyboard;
USBHIDMouse Mouse;
USBHIDConsumerControl Consumer;


// SETTINGS

const char* WIFI_SSID = "home";      // must be 2.4 GHz
const char* WIFI_PASS = "nickdaisy";
const char* HOSTNAME  = "phonehid";            // reachable as phonehid.local
const uint16_t PORT   = 7300;

// The board shouts "phonehid <ip> <port>" to the whole network on this UDP
// port every few seconds. phone.py listens for it, so the computer can find
// the board even when the .local name stops resolving.
const uint16_t BEACON_PORT = 7301;
const uint32_t BEACON_EVERY_MS = 3000;

// ---- Watchdog settings ----
// Hardware/task watchdog: reboot if loop() stops running for this long.
// Must be longer than your slowest single command (a slow swipe is ~10 s at
// the maximum step delay, but moveCounts() feeds the watchdog, so 10 is safe).
const uint32_t WDT_TIMEOUT_S = 10;
// Reboot if Wi-Fi stays disconnected this long (milliseconds).
const uint32_t WIFI_DOWN_REBOOT_MS = 180000;
// If the mouse button was left held and no command arrives for this long,
// let go of it (milliseconds).
const uint32_t HELD_RELEASE_MS = 15000;

// Runtime configuration. Python must send `screen` and `gain` after connecting.
// Zero values deliberately prevent accidental use of another phone's settings.
int   SCREEN_W = 0;
int   SCREEN_H = 0;
float PX_PER_COUNT_X = 0.0f;
float PX_PER_COUNT_Y = 0.0f;
int   REHOME_EVERY = 5;
bool  phoneConfigured = false;

// Constant-pace stepping keeps Android's pointer acceleration predictable.
// Do not change these without recalibrating.
const int STEP_COUNTS   = 2;
const int STEP_DELAY_MS = 10;

// ==========================================
// WATCHDOG
// ==========================================
inline void feed() { esp_task_wdt_reset(); }

void setupWatchdog() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t cfg;
  cfg.timeout_ms     = WDT_TIMEOUT_S * 1000;
  cfg.idle_core_mask = 0;        // do not watch the idle tasks
  cfg.trigger_panic  = true;     // reset the chip when it fires
  if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) esp_task_wdt_init(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);        // watch the loop() task
}

const char* resetName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:  return "poweron";
    case ESP_RST_SW:       return "software";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:  return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT:      return "wdt";
    case ESP_RST_BROWNOUT: return "brownout";
    default:               return "other";
  }
}


// ==========================================
// CURSOR POSITION TRACKING (same method as phone_hid.ino)
// ==========================================
float curX = 0, curY = 0;
int actionsSinceHome = 1000;     // forces a home on first use

void moveCounts(long dx, long dy, int stepDelay = STEP_DELAY_MS) {
  while (dx != 0 || dy != 0) {
    int sx = (int)constrain(dx, -STEP_COUNTS, STEP_COUNTS);
    int sy = (int)constrain(dy, -STEP_COUNTS, STEP_COUNTS);
    Mouse.move(sx, sy, 0);
    dx -= sx;
    dy -= sy;
    delay(stepDelay);
    feed();                      // long moves must not trip the watchdog
  }
}

void homeCursor() {
  for (int i = 0; i < 60; i++) {
    Mouse.move(-127, -127, 0);
    delay(8);
    feed();
  }
  curX = 0;
  curY = 0;
  actionsSinceHome = 0;
  delay(150);
}

void moveToPx(int x, int y) {
  if (actionsSinceHome >= REHOME_EVERY) homeCursor();
  x = constrain(x, 0, SCREEN_W - 1);
  y = constrain(y, 0, SCREEN_H - 1);
  long dx = lround((x - curX) / PX_PER_COUNT_X);
  long dy = lround((y - curY) / PX_PER_COUNT_Y);
  moveCounts(dx, 0);          // one axis at a time so speed matches calibration
  moveCounts(0, dy);
  curX = x;
  curY = y;
  delay(80);
}

// ==========================================
// GESTURES
// ==========================================
void tapAt(int x, int y) {
  moveToPx(x, y);
  Mouse.click(MOUSE_LEFT);
  actionsSinceHome++;
}

void doubleTapAt(int x, int y) {
  moveToPx(x, y);
  Mouse.click(MOUSE_LEFT);
  delay(100);
  Mouse.click(MOUSE_LEFT);
  actionsSinceHome++;
}

void swipePx(int x1, int y1, int x2, int y2, int stepDelay) {
  moveToPx(x1, y1);
  Mouse.press(MOUSE_LEFT);
  delay(40);
  moveCounts(lround((x2 - x1) / PX_PER_COUNT_X),
             lround((y2 - y1) / PX_PER_COUNT_Y), stepDelay);
  delay(40);
  Mouse.release(MOUSE_LEFT);
  actionsSinceHome = REHOME_EVERY;   // diagonal + different speed: position unknown
}

void scrollWheel(int clicks) {
  Mouse.move(0, 0, (int8_t)clicks);
  delay(200);
}

void tapKey(uint8_t k) {
  Keyboard.press(k);
  delay(60);
  Keyboard.releaseAll();
}

bool pressNamedKey(const String& n) {
  if (n == "enter")          tapKey(KEY_RETURN);
  else if (n == "esc" || n == "back") tapKey(KEY_ESC);
  else if (n == "tab")       tapKey(KEY_TAB);
  else if (n == "backspace") tapKey(KEY_BACKSPACE);
  else if (n == "space")     tapKey(' ');
  else if (n == "up")        tapKey(KEY_UP_ARROW);
  else if (n == "down")      tapKey(KEY_DOWN_ARROW);
  else if (n == "left")      tapKey(KEY_LEFT_ARROW);
  else if (n == "right")     tapKey(KEY_RIGHT_ARROW);
  else if (n == "gui")       tapKey(KEY_LEFT_GUI);
  else if (n == "vol_up")   { Consumer.press(CONSUMER_CONTROL_VOLUME_INCREMENT); Consumer.release(); }
  else if (n == "vol_down") { Consumer.press(CONSUMER_CONTROL_VOLUME_DECREMENT); Consumer.release(); }
  else return false;
  return true;
}

bool buttonHeld = false;         // set by the `press` command
uint32_t lastCmdMs = 0;          // time of the last command received
uint32_t wifiDrops = 0;          // how many times Wi-Fi went down since boot

void releaseAll() {
  Mouse.release(MOUSE_LEFT);
  Keyboard.releaseAll();
  buttonHeld = false;
}

// ==========================================
// NETWORK + COMMAND HANDLING
// ==========================================
WiFiServer server(PORT);
WiFiClient client;
String rx;
bool wifiUp = false;
bool serverStarted = false;

void reply(const String& s) {
  if (client && client.connected()) client.println(s);
}

void handle(String line) {
  line.trim();
  if (line.length() == 0) return;
  lastCmdMs = millis();
  const char* s = line.c_str();
  int a, b, c, d, e = 10;
  float f, g;

  // Commands that depend on screen coordinates are rejected until Python has
  // supplied valid dimensions and calibrated gains. Config/status commands remain available.
  if (!phoneConfigured && (
      line.startsWith("tap ") || line.startsWith("dtap ") ||
      line.startsWith("swipe ") || line.startsWith("move ")
  )) {
    reply("err phone not configured; send screen and gain first");
    return;
  }

  if (line == "ping") {
    reply("ok");
  } else if (line == "capabilities") {
    reply("ok firmware=2 hid=mouse,keyboard,consumer config=runtime screen=required gain=required");
  } else if (line == "home") {
    homeCursor();
    reply("ok");
  } else if (sscanf(s, "tap %d %d", &a, &b) == 2) {
    tapAt(a, b);
    reply("ok");
  } else if (sscanf(s, "dtap %d %d", &a, &b) == 2) {
    doubleTapAt(a, b);
    reply("ok");
  } else if (sscanf(s, "swipe %d %d %d %d %d", &a, &b, &c, &d, &e) >= 4) {
    swipePx(a, b, c, d, constrain(e, 2, 50));
    reply("ok");
  } else if (sscanf(s, "scroll %d", &a) == 1) {
    scrollWheel(constrain(a, -127, 127));
    reply("ok");
  } else if (line.startsWith("type ")) {
    Keyboard.print(line.substring(5));
    reply("ok");
  } else if (line.startsWith("key ")) {
    String name = line.substring(4);
    name.trim();
    if (pressNamedKey(name)) reply("ok"); else reply("err unknown key");
  } else if (line == "press") {
    Mouse.press(MOUSE_LEFT);
    buttonHeld = true;
    reply("ok");
  } else if (line == "release") {
    Mouse.release(MOUSE_LEFT);
    buttonHeld = false;
    reply("ok");
  } else if (line == "click") {
    Mouse.click(MOUSE_LEFT);
    reply("ok");
  } else if (sscanf(s, "move %d %d", &a, &b) == 2) {
    moveCounts(a, 0);
    moveCounts(0, b);
    curX = constrain(curX + a * PX_PER_COUNT_X, 0, SCREEN_W - 1);
    curY = constrain(curY + b * PX_PER_COUNT_Y, 0, SCREEN_H - 1);
    reply("ok");
  } else if (line == "pos") {
    reply("ok " + String((int)curX) + " " + String((int)curY));
  } else if (line == "ip") {
    reply("ok " + WiFi.localIP().toString());
  } else if (line == "status") {
    // Why did it drop out? reset= tells you what caused the last reboot:
    // brownout = power problem, task_wdt = firmware hung, poweron = just plugged in.
    reply("ok uptime=" + String(millis() / 1000) +
          " reset=" + String(resetName(esp_reset_reason())) +
          " rssi=" + String(WiFi.RSSI()) +
          " wifidrops=" + String(wifiDrops) +
          " heap=" + String(ESP.getFreeHeap()) +
          " configured=" + String(phoneConfigured ? 1 : 0) +
          " screen=" + String(SCREEN_W) + "x" + String(SCREEN_H) +
          " gain=" + String(PX_PER_COUNT_X, 3) + "," + String(PX_PER_COUNT_Y, 3));
  } else if (line == "reboot") {
    reply("ok");
    delay(200);
    releaseAll();
    ESP.restart();
  } else if (line == "hang") {
    // TESTING ONLY: freezes the firmware so you can watch the watchdog reboot it.
    reply("ok");
    while (true) { }
  } else if (
      sscanf(s, "screen %d %d", &a, &b) == 2 &&
      a >= 200 && a <= 20000 && b >= 200 && b <= 20000
  ) {
    SCREEN_W = a;
    SCREEN_H = b;
    curX = 0;
    curY = 0;
    actionsSinceHome = 1000;  // force a fresh home after reconfiguration
    phoneConfigured = (PX_PER_COUNT_X > 0.0f && PX_PER_COUNT_Y > 0.0f);
    reply("ok");
  } else if (
      sscanf(s, "gain %f %f", &f, &g) == 2 &&
      isfinite(f) && isfinite(g) &&
      f >= 0.1f && f <= 100.0f && g >= 0.1f && g <= 100.0f
  ) {
    PX_PER_COUNT_X = f;
    PX_PER_COUNT_Y = g;
    phoneConfigured = (SCREEN_W > 0 && SCREEN_H > 0);
    reply("ok");
  } else if (sscanf(s, "rehome %d", &a) == 1 && a >= 1 && a <= 100) {
    REHOME_EVERY = a;
    reply("ok");
  } else {
    reply("err unknown command");
  }
}

void wifiTick() {
  static uint32_t lastTry = 0;
  static uint32_t downSince = 0;
  bool connected = (WiFi.status() == WL_CONNECTED);

  if (connected && !wifiUp) {
    wifiUp = true;
    MDNS.end();
    MDNS.begin(HOSTNAME);
    if (!serverStarted) { server.begin(); serverStarted = true; }
  }
  if (!connected && wifiUp) {
    wifiUp = false;
    wifiDrops++;
    releaseAll();
  }

  // Link watchdog: if Wi-Fi stays down too long, a clean reboot is the most
  // reliable way back onto the network.
  if (connected) {
    downSince = 0;
  } else {
    if (downSince == 0) downSince = millis();
    if (millis() - downSince > WIFI_DOWN_REBOOT_MS) {
      releaseAll();
      delay(100);
      ESP.restart();
    }
  }

  if (!connected && millis() - lastTry > 15000) {
    lastTry = millis();
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

WiFiUDP beaconUdp;

void beaconTick() {
  static uint32_t last = 0;
  if (!wifiUp || millis() - last < BEACON_EVERY_MS) return;
  last = millis();
  IPAddress ip = WiFi.localIP();
  IPAddress mask = WiFi.subnetMask();
  IPAddress bc(ip[0] | (uint8_t)~mask[0], ip[1] | (uint8_t)~mask[1],
               ip[2] | (uint8_t)~mask[2], ip[3] | (uint8_t)~mask[3]);
  beaconUdp.beginPacket(bc, BEACON_PORT);
  beaconUdp.print("phonehid " + ip.toString() + " " + String(PORT));
  beaconUdp.endPacket();
}

void pollClient() {
  if (wifiUp) {
    WiFiClient nc = server.accept();       // on older cores use server.available()
    if (nc) {
      if (client) client.stop();
      client = nc;
      client.setNoDelay(true);
      rx = "";
    }
  }
  if (client && !client.connected()) {
    client.stop();
    releaseAll();                          // never leave the button held
  }
  if (client) {
    while (client.available()) {
      char ch = client.read();
      if (ch == '\n') { handle(rx); rx = ""; }
      else if (ch != '\r' && rx.length() < 200) rx += ch;
    }
  }
}

void setup() {
  Keyboard.begin();
  Mouse.begin();
  Consumer.begin();
  USB.begin();

  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setSleep(false);                    // lower latency
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  lastCmdMs = millis();
  setupWatchdog();                         // last, so setup() itself is not watched
}

void loop() {
  feed();                                  // proves loop() is still running
  wifiTick();
  beaconTick();
  pollClient();

  // Safety: a held mouse button with nobody commanding it gets released.
  if (buttonHeld && millis() - lastCmdMs > HELD_RELEASE_MS) {
    Mouse.release(MOUSE_LEFT);
    buttonHeld = false;
  }
  delay(1);
}
