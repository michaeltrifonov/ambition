"""Client-side implementation of the Anthropic computer-use tool.

Translates the model's abstract actions (screenshot / click / type / key /
scroll / zoom / ...) into real X11 operations via scrot + xdotool against the
VNC/Xvfb display the desktop runs on. Returns tool_result content blocks
(text and/or a fresh screenshot so the model sees the effect of its action).
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time

from . import constants as C

_ENV = {**os.environ, "DISPLAY": C.DISPLAY}
_MAX_WAIT_S = 10.0  # cap on the `wait` / `hold_key` durations we'll honor


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, env=_ENV, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]} (is it installed on the desktop?)"


_GEOMETRY: tuple[int, int] | None = None


def get_geometry() -> tuple[int, int]:
    """Live (width, height) of the display, detected via xdotool and cached.

    Falls back to the configured UAA_DISPLAY_WIDTH/HEIGHT if detection fails, so
    the agent matches whatever desktop is actually running rather than a hardcode.
    """
    global _GEOMETRY
    if _GEOMETRY is not None:
        return _GEOMETRY
    rc, out = _run(["xdotool", "getdisplaygeometry"])
    if rc == 0 and out:
        try:
            w, h = out.split()[:2]
            _GEOMETRY = (int(w), int(h))
            return _GEOMETRY
        except (ValueError, IndexError):
            pass
    from .log import get_logger
    get_logger("uaa.agent", C.AGENT_LOG).warning(
        "could not detect display geometry on %s (display may be unreachable) — "
        "falling back to %sx%s", C.DISPLAY, C.DISPLAY_WIDTH, C.DISPLAY_HEIGHT)
    _GEOMETRY = (C.DISPLAY_WIDTH, C.DISPLAY_HEIGHT)
    return _GEOMETRY


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _image(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def _capture_png(path: str) -> bool:
    """Capture the full display to `path`. Try scrot, then ImageMagick import."""
    for cmd in (["scrot", "--overwrite", "--pointer", path], ["scrot", path],
                ["import", "-window", "root", path]):
        rc, _ = _run(cmd)
        if rc == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    return False


def _screenshot_b64() -> str | None:
    path = os.path.join(tempfile.gettempdir(), "uaa_shot.png")
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    if not _capture_png(path):
        return None
    with open(path, "rb") as fh:
        return base64.standard_b64encode(fh.read()).decode("ascii")


def _zoom_b64(region) -> str | None:
    path = os.path.join(tempfile.gettempdir(), "uaa_zoom.png")
    if not _capture_png(path):
        return None
    try:
        from PIL import Image

        x1, y1, x2, y2 = region
        with Image.open(path) as im:
            crop = im.crop((int(x1), int(y1), int(x2), int(y2)))
            out = os.path.join(tempfile.gettempdir(), "uaa_zoom_crop.png")
            crop.save(out)
        with open(out, "rb") as fh:
            return base64.standard_b64encode(fh.read()).decode("ascii")
    except Exception:
        # No Pillow (or bad region) — fall back to the full screenshot.
        with open(path, "rb") as fh:
            return base64.standard_b64encode(fh.read()).decode("ascii")


def _modifiers(text: str | None) -> list[str]:
    if not text:
        return []
    return [k.strip() for k in text.replace("+", " ").split() if k.strip()]


def _in_bounds(coord) -> bool:
    try:
        x, y = coord
    except (TypeError, ValueError):
        return False
    # Valid pixels of a WxH display are [0, W-1] x [0, H-1].
    w, h = get_geometry()
    return 0 <= x < w and 0 <= y < h


def _click_cmd(coord, button: str, repeat: int, text: str | None) -> list[str]:
    x, y = coord
    cmd = ["xdotool", "mousemove", "--sync", str(int(x)), str(int(y))]
    mods = _modifiers(text)
    for m in mods:
        cmd += ["keydown", m]
    cmd += ["click", "--repeat", str(repeat), button]
    for m in reversed(mods):
        cmd += ["keyup", m]
    return cmd


def execute(inp: dict) -> tuple[list[dict], bool]:
    """Run one computer action. Returns (content_blocks, is_error)."""
    action = inp.get("action")

    # ---- pure observation ----
    if action == "screenshot":
        b64 = _screenshot_b64()
        if b64 is None:
            return [_text("Error: screenshot failed. Display may be unavailable.")], True
        return [_image(b64)], False

    if action == "zoom":
        region = inp.get("region")
        if not region or len(region) != 4:
            return [_text("Error: zoom requires region [x1,y1,x2,y2].")], True
        x1, y1, x2, y2 = region
        gw, gh = get_geometry()
        if not (0 <= x1 < x2 <= gw and 0 <= y1 < y2 <= gh):
            return [_text(f"Error: zoom region {region} invalid for "
                          f"{gw}x{gh} (need 0<=x1<x2<=W, 0<=y1<y2<=H).")], True
        b64 = _zoom_b64(region)
        if b64 is None:
            return [_text("Error: zoom capture failed.")], True
        return [_image(b64)], False

    if action == "cursor_position":
        rc, out = _run(["xdotool", "getmouselocation", "--shell"])
        return [_text(out if rc == 0 else "Error reading cursor position.")], rc != 0

    # ---- actions that need a cursor coordinate ----
    coord = inp.get("coordinate")
    cmd: list[str] | None = None

    if action == "mouse_move":
        if not _in_bounds(coord):
            return [_text(f"Error: {coord} out of bounds {get_geometry()[0]}x{get_geometry()[1]}.")], True
        cmd = ["xdotool", "mousemove", "--sync", str(int(coord[0])), str(int(coord[1]))]

    elif action in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        if not _in_bounds(coord):
            return [_text(f"Error: {coord} out of bounds {get_geometry()[0]}x{get_geometry()[1]}.")], True
        button = {"left_click": "1", "right_click": "3", "middle_click": "2",
                  "double_click": "1", "triple_click": "1"}[action]
        repeat = {"double_click": 2, "triple_click": 3}.get(action, 1)
        cmd = _click_cmd(coord, button, repeat, inp.get("text"))

    elif action in ("left_mouse_down", "left_mouse_up"):
        verb = "mousedown" if action == "left_mouse_down" else "mouseup"
        cmd = ["xdotool", verb, "1"]

    elif action == "left_click_drag":
        start = inp.get("start_coordinate")
        if not _in_bounds(start) or not _in_bounds(coord):
            return [_text("Error: drag coordinates out of bounds.")], True
        mods = _modifiers(inp.get("text"))
        cmd = ["xdotool"]
        for m in mods:
            cmd += ["keydown", m]
        cmd += ["mousemove", "--sync", str(int(start[0])), str(int(start[1])),
                "mousedown", "1",
                "mousemove", "--sync", str(int(coord[0])), str(int(coord[1])),
                "mouseup", "1"]
        for m in reversed(mods):
            cmd += ["keyup", m]

    elif action == "scroll":
        if coord and not _in_bounds(coord):
            return [_text(f"Error: {coord} out of bounds.")], True
        direction = inp.get("scroll_direction", "down")
        amount = int(inp.get("scroll_amount", 3))
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(direction, "5")
        cmd = ["xdotool"]
        if coord:
            cmd += ["mousemove", "--sync", str(int(coord[0])), str(int(coord[1]))]
        mods = _modifiers(inp.get("text"))
        for m in mods:
            cmd += ["keydown", m]
        cmd += ["click", "--repeat", str(max(1, amount)), button]
        for m in reversed(mods):
            cmd += ["keyup", m]

    elif action == "type":
        text = inp.get("text", "")
        cmd = ["xdotool", "type", "--delay", "12", "--", text]

    elif action == "key":
        keys = inp.get("text", "")
        cmd = ["xdotool", "key", "--"] + keys.split()

    elif action == "hold_key":
        keys = _modifiers(inp.get("text"))
        duration = min(float(inp.get("duration", 1)), _MAX_WAIT_S)
        if not keys:
            return [_text("Error: hold_key requires text (the key to hold).")], True
        cmd = ["xdotool"]
        for k in keys:
            cmd += ["keydown", k]
        cmd += ["sleep", str(duration)]
        for k in reversed(keys):
            cmd += ["keyup", k]

    elif action == "wait":
        time.sleep(min(float(inp.get("duration", 1)), _MAX_WAIT_S))
        b64 = _screenshot_b64()
        return ([_image(b64)] if b64 else [_text("waited")]), False

    else:
        return [_text(f"Error: unsupported action '{action}'.")], True

    rc, out = _run(cmd)
    if rc != 0:
        return [_text(f"Error performing {action}: {out}")], True

    # Give the UI a moment, then return a fresh screenshot so the model sees the result.
    time.sleep(C.SCREENSHOT_SETTLE_S)
    b64 = _screenshot_b64()
    if b64 is None:
        return [_text(f"{action} done (screenshot unavailable).")], False
    return [_image(b64)], False
