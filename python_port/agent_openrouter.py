#!/usr/bin/env python3
"""Android phone agent using OpenRouter. Self-contained: needs only phone.py
next to it. No Anthropic package or key involved.

    pip install openai pillow

Set these in the SAME terminal you run the script from:

    Linux/macOS:
        export OPENROUTER_API_KEY=sk-or-...
        export OPENROUTER_MODEL=provider/model-name:free

    Windows PowerShell:
        $env:OPENROUTER_API_KEY="sk-or-..."
        $env:OPENROUTER_MODEL="provider/model-name:free"

Run:
    python agent_openrouter.py "Open Settings and tell me the Android version"

Optional:
    GRID=1        draw a labeled coordinate grid on screenshots (helps weak models aim)
    MAX_STEPS=25  step limit
"""
import base64
import io
import json
import os
import sys
import time

from openai import (OpenAI, RateLimitError, APIConnectionError,
                    APITimeoutError, APIStatusError)
from PIL import Image, ImageDraw

from phone import Phone, PhoneError

# ---------- config ----------
API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL = os.environ.get("OPENROUTER_MODEL")
BASE_URL = "https://openrouter.ai/api/v1"
MAX_STEPS = int(os.environ.get("MAX_STEPS", "25"))
USE_GRID = os.environ.get("GRID") == "1"
MAX_EDGE = 1280      # long edge of the image the model sees
SETTLE = 0.8         # seconds to let the UI settle before the next screenshot

if not API_KEY or not MODEL:
    sys.exit("Set OPENROUTER_API_KEY and OPENROUTER_MODEL first (see the top of this file).")

client = OpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers={"HTTP-Referer": "http://localhost", "X-Title": "phone-agent"},
)

# ---------- tools ----------
def _tool(name, description, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       "required": required or []}}}

INT = {"type": "integer"}
TOOLS = [
    _tool("tap", "Tap at pixel (x, y) of the screenshot.", {"x": INT, "y": INT}, ["x", "y"]),
    _tool("double_tap", "Double tap at (x, y).", {"x": INT, "y": INT}, ["x", "y"]),
    _tool("swipe", "Swipe from (x1,y1) to (x2,y2). To scroll a list down, swipe upward.",
          {"x1": INT, "y1": INT, "x2": INT, "y2": INT}, ["x1", "y1", "x2", "y2"]),
    _tool("type_text", "Type ASCII text into the focused field. Tap the field first.",
          {"text": {"type": "string"}}, ["text"]),
    _tool("key", "Press a key: enter, esc, back, tab, backspace, space, up, down, left, right, vol_up, vol_down.",
          {"name": {"type": "string"}}, ["name"]),
    _tool("home", "Press the Android Home button."),
    _tool("back", "Press the Android Back button."),
    _tool("wait", "Wait 2 seconds (for loading screens), then look again."),
    _tool("done", "Call when the task is finished or cannot be completed.",
          {"success": {"type": "boolean"}, "summary": {"type": "string"}},
          ["success", "summary"]),
]

SYSTEM = """You control a real Android phone through screenshots and touch/keyboard tools.
After every action you are shown a fresh screenshot. Look at it carefully before acting.
- Coordinates are integer pixels in the screenshot you are shown (top-left is 0,0). Never use fractions or percentages.
- ALWAYS respond by calling exactly one tool. Do one action at a time, then check the result.
- Do not assume a tap worked. If the screen did not change, try something different.
- Never enter passwords, payment details, or send messages/purchases unless the task explicitly says so.
- Call `done` as soon as the task is complete, or if you are stuck."""


# ---------- screenshots ----------
def shrink(png):
    """Returns (jpeg_b64, scale, (w, h)); scale = shown_size / real_size."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    scale = min(1.0, MAX_EDGE / max(im.size))
    if scale < 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)),
                       Image.LANCZOS)
    if USE_GRID:
        draw = ImageDraw.Draw(im)
        for x in range(0, im.width, 100):
            draw.line([(x, 0), (x, im.height)], fill=(255, 0, 0), width=1)
            draw.text((x + 2, 2), str(x), fill=(255, 0, 0))
        for y in range(0, im.height, 100):
            draw.line([(0, y), (im.width, y)], fill=(0, 0, 255), width=1)
            draw.text((2, y + 2), str(y), fill=(0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode(), scale, im.size


def img_block(b64):
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def prune_old_images(messages):
    """Keep only the newest screenshot; older ones just cost tokens."""
    seen = False
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            for i, c in enumerate(m["content"]):
                if c.get("type") == "image_url":
                    if seen:
                        m["content"][i] = {"type": "text", "text": "[older screenshot omitted]"}
                    seen = True


# ---------- phone actions ----------
def coords(a, names, scale, shown):
    """Model coords -> real phone pixels. Accepts numbers or numeric strings.
    If every value is between 0 and 1 the model gave fractions of the screen,
    so they are multiplied by the screenshot size first."""
    vals = [float(a[n]) for n in names]
    frac = all(0.0 <= v <= 1.0 for v in vals)
    out = []
    for n, v in zip(names, vals):
        axis = 0 if n.startswith("x") else 1
        out.append((v * shown[axis] if frac else v) / scale)
    return out


def execute(phone, name, a, scale, shown):
    if name == "tap":
        phone.tap(*coords(a, ["x", "y"], scale, shown))
    elif name == "double_tap":
        phone.double_tap(*coords(a, ["x", "y"], scale, shown))
    elif name == "swipe":
        phone.swipe(*coords(a, ["x1", "y1", "x2", "y2"], scale, shown))
    elif name == "type_text":
        phone.type_text(str(a["text"]))
    elif name == "key":
        phone.key(str(a["name"]))
    elif name == "home":
        phone.home_button()
    elif name == "back":
        phone.back_button()
    elif name == "wait":
        time.sleep(2)
    else:
        raise PhoneError(f"unknown tool {name}")


# ---------- model call ----------
def chat(messages):
    """Call OpenRouter with retries for rate limits and temporary errors.
    (Only the model call is retried; phone actions are never replayed.)"""
    last = None
    for attempt in range(6):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, max_tokens=1024)
            if resp.choices:
                return resp.choices[0].message
            last = RuntimeError(f"empty response: {resp}")
        except (RateLimitError, APIConnectionError, APITimeoutError) as e:
            last = e
        except APIStatusError as e:
            if e.status_code >= 500:
                last = e
            else:
                raise     # 400/401/402/404: bad key, model id, credits, or no tool/vision support
        wait = 2 ** attempt * 2
        print(f"  model call failed ({last}); retrying in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"model call kept failing: {last}")


# ---------- main loop ----------
def run(task):
    phone = Phone()
    b64, scale, shown = shrink(phone.screenshot())
    sw, sh = shown
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": f"Task: {task}\nThe screenshot is {sw}x{sh} pixels. "
                                     f"Give coordinates as integer PIXELS (x 0-{sw}, y 0-{sh}), not fractions."},
            img_block(b64)]},
    ]

    for step in range(1, MAX_STEPS + 1):
        prune_old_images(messages)
        msg = chat(messages)

        if msg.content and msg.content.strip():
            print(f"[{step}] {msg.content.strip()}")

        if not msg.tool_calls:
            print("model replied without calling a tool; stopping.")
            return False

        # Rebuild the assistant message by hand so only standard fields are sent back.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name,
                                         "arguments": c.function.arguments or "{}"}}
                           for c in msg.tool_calls],
        })

        for c in msg.tool_calls:
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": "Invalid JSON arguments. Try again."})
                continue
            print(f"[{step}] {name} {args}")

            if name == "done":
                print("DONE:", args.get("summary"))
                return bool(args.get("success"))

            note = "ok"
            try:
                execute(phone, name, args, scale, shown)
            except (PhoneError, KeyError, ValueError) as e:
                # Never replay: the action may already have happened.
                note = f"error: {e}. The action may or may not have happened; check the screen."
                print("  ", note)
                if isinstance(e, PhoneError):
                    try:
                        phone.reconnect()
                    except PhoneError:
                        pass
            messages.append({"role": "tool", "tool_call_id": c.id, "content": note})

        # Tool messages must be text, so the screenshot goes in a user message.
        time.sleep(SETTLE)
        b64, scale, shown = shrink(phone.screenshot())
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"Current screen ({shown[0]}x{shown[1]} pixels; "
                                     f"use integer pixel coordinates):"},
            img_block(b64)]})

    print("hit step limit")
    return False


if __name__ == "__main__":
    print(f"model: {MODEL}  grid: {USE_GRID}")
    task = " ".join(sys.argv[1:]) or "Describe what is on the screen, then call done."
    try:
        run(task)
    except PhoneError as e:
        print("phone error:", e)
        sys.exit(1)