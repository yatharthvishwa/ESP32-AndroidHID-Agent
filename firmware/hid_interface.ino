#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDMouse.h"
#include "USBHIDConsumerControl.h"

USBHIDKeyboard Keyboard;
USBHIDMouse Mouse;
USBHIDConsumerControl Consumer;

// ==========================================
// SETTINGS
// ==========================================
// 0 = CALIBRATE (measure how far the cursor moves per count)
// 1 = VERIFY    (visit corners/center so you can check positions)
// 2 = DEMO      (your original automation, now using pixel positions)
#define MODE 2

// Your phone's screen size in pixels. Find it with: adb shell wm size
const int SCREEN_W = 720;    // from the touchscreen range in getevent (mtk-tpd)
const int SCREEN_H = 1600;   // confirm with: adb shell wm size

// Filled in after calibration: pixels moved per mouse "count".
// Start at 1.0, then replace with the values MODE 0 helps you measure.
float PX_PER_COUNT_X = 3.81;
float PX_PER_COUNT_Y = 3.81;

// Movement is sent in small constant-pace steps so Android's pointer
// acceleration stays predictable. Do not change these after calibrating
// (or recalibrate if you do).
const int STEP_COUNTS   = 2;     // 2 counts every 10 ms = 200 counts/s
const int STEP_DELAY_MS = 10;

// Distance used by calibration. Kept small so the cursor does not hit the
// screen edge (which would make the reading wrong).
const int CAL_COUNTS = 100;

// Re-home the cursor into the top-left corner every N actions to
// remove accumulated drift.
const int REHOME_EVERY = 5;

//cursor tracking

float curX = 0, curY = 0;                     // estimated position in pixels
int actionsSinceHome = REHOME_EVERY;          // forces a home on first use

// Move by a number of mouse counts at a constant pace.
void moveCounts(long dx, long dy, int stepDelay = STEP_DELAY_MS) {
  while (dx != 0 || dy != 0) {
    int sx = (int)constrain(dx, -STEP_COUNTS, STEP_COUNTS);
    int sy = (int)constrain(dy, -STEP_COUNTS, STEP_COUNTS);
    Mouse.move(sx, sy, 0);
    dx -= sx;
    dy -= sy;
    delay(stepDelay);
  }
}

// Slam the cursor into the top-left corner. The screen edge stops it,
// so this always gives a known position (0,0) no matter where it was.
void homeCursor() {
  for (int i = 0; i < 60; i++) {
    Mouse.move(-127, -127, 0);
    delay(8);
  }
  curX = 0;
  curY = 0;
  actionsSinceHome = 0;
  delay(150);
}

// Move the cursor to a pixel position on the screen.
void moveToPx(int x, int y) {
  if (actionsSinceHome >= REHOME_EVERY) homeCursor();
  x = constrain(x, 0, SCREEN_W - 1);
  y = constrain(y, 0, SCREEN_H - 1);
  long dx = lround((x - curX) / PX_PER_COUNT_X);
  long dy = lround((y - curY) / PX_PER_COUNT_Y);
  // Move one axis at a time. Android's pointer acceleration depends on speed,
  // and a diagonal move is faster than the single-axis moves used to calibrate.
  moveCounts(dx, 0);
  moveCounts(0, dy);
  curX = x;
  curY = y;
  delay(80);
}

// GESTURES

// Hold the left button down for a while at the current spot. Pointer location
// draws a pressed pointer and shows its X/Y even when no cursor is visible.
void markHere(int holdMs) {
  Mouse.press(MOUSE_LEFT);
  delay(holdMs);
  Mouse.release(MOUSE_LEFT);
}

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

// Press-and-drag from one point to another. Whether an Android app treats a
// mouse drag as a swipe varies, so test it; scrollWheel() is the fallback.
void swipePx(int x1, int y1, int x2, int y2, int stepDelay = 10) {
  moveToPx(x1, y1);
  Mouse.press(MOUSE_LEFT);
  delay(40);
  moveCounts(lround((x2 - x1) / PX_PER_COUNT_X),
             lround((y2 - y1) / PX_PER_COUNT_Y), stepDelay);
  delay(40);
  Mouse.release(MOUSE_LEFT);
  // A different pace changes acceleration, so the position is now uncertain.
  actionsSinceHome = REHOME_EVERY;
}

// Mouse wheel scroll: negative = scroll content up, positive = down.
void scrollWheel(int clicks) {
  Mouse.move(0, 0, (int8_t)clicks);
  delay(200);
}

void openPhoneSearch() {
  Keyboard.press(KEY_LEFT_GUI);
  delay(100);
  Keyboard.releaseAll();
}

void pressBack() {
  Keyboard.press(KEY_ESC);
  delay(100);
  Keyboard.releaseAll();
}

// ==========================================
// MODE 0: CALIBRATION
// ==========================================
// Turn on Developer options -> "Pointer location" first. It shows the live
// X and Y of the pointer at the top of the screen.
// The cursor goes to the corner, moves exactly 500 counts to the right,
// then waits 10 s. Read X. Then it does the same downward. Read Y.
//   PX_PER_COUNT_X = (X you read) / CAL_COUNTS   (CAL_COUNTS = 100)
//   PX_PER_COUNT_Y = (Y you read) / CAL_COUNTS
void runCalibration() {
  homeCursor();
  delay(2000);
  moveCounts(CAL_COUNTS, 0);       // X test
  markHere(10000);          // hold the button: read X on Pointer location
  delay(1000);

  homeCursor();
  delay(2000);
  moveCounts(0, CAL_COUNTS);       // Y test
  markHere(10000);          // hold the button: read Y on Pointer location
  delay(1000);
}

// ==========================================
// MODE 1: VERIFY
// ==========================================
// Visits five points. Compare where the cursor stops with the target
// and the X/Y shown by Pointer location.
void runVerify() {
  const int m = 100;  // margin from the edges
  const int pts[5][2] = {
    {m, m}, {SCREEN_W - m, m}, {SCREEN_W / 2, SCREEN_H / 2},
    {m, SCREEN_H - m}, {SCREEN_W - m, SCREEN_H - m}
  };
  for (int i = 0; i < 5; i++) {
    homeCursor();                 // re-home each time to test absolute accuracy
    moveToPx(pts[i][0], pts[i][1]);
    markHere(4000);               // hold the button so Pointer location draws it
    delay(1000);
  }
}

// ==========================================
// MODE 2: DEMO (your original sequence)
// ==========================================
void runDemo() {
  Consumer.press(CONSUMER_CONTROL_VOLUME_INCREMENT);
  Consumer.release();
  delay(1000);

  openPhoneSearch();
  delay(1500);

  Keyboard.print("Notes");
  delay(1000);
  // Keyboard.write(KEY_RETURN);
  tapAt(100,260);
  delay(3000);

  tapAt(600, 1334); //tap at plus in notes
  delay(3000);

  tapAt(589, 1197); 
  delay(3000);

  while (true) {
    Keyboard.println("HELLO");
    delay(2000);
  }
  // Keyboard.println("HELLO");
  // delay(2000);

  // scrollWheel(-3);                          // scroll by wheel
  // delay(1000);
  // swipePx(SCREEN_W / 2, SCREEN_H * 3 / 4,   // swipe up from lower-middle
  //         SCREEN_W / 2, SCREEN_H / 4);
  // delay(2000);

  // doubleTapAt(SCREEN_W / 2, SCREEN_H / 2);  // double tap the center
  // delay(2000);

  // pressBack();
  // delay(30000);
}

// ==========================================
void setup() {
  Keyboard.begin();
  Mouse.begin();
  Consumer.begin();
  USB.begin();
  delay(8000);   // time to plug into the phone
}

void loop() {
#if MODE == 0
  runCalibration();
#elif MODE == 1
  runVerify();
#else
  runDemo();
#endif
  delay(3000);
}