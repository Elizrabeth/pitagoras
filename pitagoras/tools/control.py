"""Mouse & keyboard control, so Pitagoras can click through things and play simple games
(e.g. FNAF 1: click the door/light buttons, open the camera, …).

Coordinates are pixels in the screenshot Pitagoras sees (look_at_screen with grid=true),
mapped back to the real screen. Every action returns a fresh screenshot so it sees the result.

Safety:
  * Settings > "Let Pitagoras use mouse & keyboard" turns it all off.
  * Move the mouse yourself while it's in control and it stops immediately (you took over).
  * "Stop talking" / a new message interrupts it, and a session has a step limit (control.max_steps).
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from . import ctx, tool

log = logging.getLogger("control")

user32 = ctypes.windll.user32
try:  # real pixels, so clicks land exactly where the (physical-pixel) screenshot shows
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
MOUSE = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
MOUSEEVENTF_WHEEL = 0x800
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, data: int = 0) -> INPUT:
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(0, 0, data, flags, 0, 0)
    return i


VK = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "tab": 0x09, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "shift": 0x10, "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "win": 0x5B, "windows": 0x5B, "capslock": 0x14, "printscreen": 0x2C,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}


def _vk(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        r = user32.VkKeyScanW(ord(n))
        if r != -1:
            return r & 0xFF
    raise ValueError(f"unknown key '{name}'")


def _key(vk: int, up: bool) -> INPUT:
    """Hardware scan codes: games that ignore virtual keys still get these."""
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(0 if scan else vk, scan, flags if scan else flags & ~KEYEVENTF_SCANCODE, 0, 0)
    return i


# ── safety: stop when the user grabs the mouse ────────────────
_last_pos: tuple[int, int] | None = None
_last_move = 0.0


class TookOver(Exception):
    pass


def reset() -> None:
    """New request: forget where we left the mouse last time (you've used it since)."""
    global _last_pos
    _last_pos = None


def _cursor() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _check_user() -> None:
    """If the mouse isn't where we left it, the user moved it: stop."""
    if _last_pos is None or time.monotonic() - _last_move > 120:
        return
    x, y = _cursor()
    if abs(x - _last_pos[0]) + abs(y - _last_pos[1]) > 60:
        raise TookOver


def _move(x: int, y: int) -> None:
    global _last_pos, _last_move
    user32.SetCursorPos(int(x), int(y))
    _last_pos, _last_move = (int(x), int(y)), time.monotonic()


def _allowed() -> str | None:
    c = (ctx.cfg or {}).get("control") or {}
    if not c.get("enabled", True):
        return "Mouse & keyboard control is turned off (right-click Pitagoras > Settings)."
    return None


def _after(text: str, wait: float = 0.35) -> dict | str:
    """Result + a fresh screenshot (with grid) so the model sees what happened."""
    time.sleep(wait)
    from ..eyes import GRID, GRID_NOTE, LAST, can_see, capture_for_control

    if not can_see()[0]:
        return text
    img = capture_for_control(ctx.extra.get("char_pos"))
    return {"text": text + " Here is the screen now." + GRID_NOTE.format(w=LAST["w"], h=LAST["h"], g=GRID), "image": img}


def _guard(fn):
    """Common checks around every control action."""
    def run(*a, **k):
        why = _allowed()
        if why:
            return why
        try:
            _check_user()
            return fn(*a, **k)
        except TookOver:
            ctx.extra["control_stopped"] = True
            return "STOPPED: the user moved the mouse and took over. Stop controlling the PC now."
        except ValueError as e:
            return f"Error: {e}"
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    run.__wrapped__ = fn
    import inspect

    run.__signature__ = inspect.signature(fn)
    return run


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _in_shot(x, y) -> tuple[float, float]:
    """Keep clicks inside the area of the last screenshot (a bad guess can't hit some other app)."""
    from ..eyes import LAST

    w, h = LAST.get("w") or 0, LAST.get("h") or 0
    x, y = _num(x), _num(y)
    if w and h:
        x, y = min(max(x, 0), w - 1), min(max(y, 0), h - 1)
    return x, y


# ── finding things by name (much more reliable than guessing coordinates) ────
def _fold(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in s if c.isalnum()).lower()


def _score(want: str, have: str) -> int:
    if not want or not have:
        return 0
    if have == want:
        return 3
    if have.startswith(want) or want.startswith(have) and len(have) >= 3:
        return 2
    return 1 if want in have else 0


INTERACTIVE = {"Button", "Hyperlink", "MenuItem", "ListItem", "TabItem", "TreeItem", "CheckBox", "RadioButton",
               "SplitButton", "ComboBox", "Image", "DataItem"}


def _uia_find(target: str, hwnd: int, box: dict) -> tuple[int, int, str] | None:
    """A control in the window whose accessible name matches (buttons, links, tabs, icons…)."""
    from pywinauto import Desktop

    want = _fold(target)
    best = None
    t0 = time.monotonic()
    win = Desktop(backend="uia").window(handle=hwnd)
    for el in win.descendants():
        if time.monotonic() - t0 > 4:
            break
        try:
            name = el.window_text() or ""
            s = _score(want, _fold(name))
            if not s:
                continue
            r = el.rectangle()
            if r.width() < 4 or r.height() < 4 or not (box["left"] <= r.mid_point().x <= box["left"] + box["width"]):
                continue
            kind = el.element_info.control_type or ""
            rank = (s, kind in INTERACTIVE, -len(name))
            if best is None or rank > best[0]:
                best = (rank, r.mid_point().x, r.mid_point().y, f"{kind} '{name}'")
        except Exception:  # noqa: BLE001
            continue
    return best[1:] if best else None


def _ocr_find(target: str, box: dict) -> tuple[int, int, str] | None:
    """Read the text on screen (Windows OCR) and find the words; works in games too."""
    import asyncio
    import io

    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab({k: int(box[k]) for k in ("left", "top", "width", "height")})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    zoom = 2 if img.width < 1400 else 1  # small text reads better enlarged
    if zoom > 1:
        img = img.resize((img.width * zoom, img.height * zoom), Image.LANCZOS)
    png = io.BytesIO()
    img.save(png, "PNG")

    async def recognize():
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        w = DataWriter(stream)
        w.write_bytes(png.getvalue())
        await w.store_async()
        stream.seek(0)
        bmp = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        return await engine.recognize_async(bmp)

    result = asyncio.run(recognize())
    want = _fold(target)
    n_words = max(1, len(target.split()))
    best = None
    for line in result.lines:
        words = list(line.words)
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + n_words + 1) + 1):
                text = " ".join(w.text for w in words[i:j])
                s = _score(want, _fold(text))
                if s and (best is None or (s, -len(text)) > best[0]):
                    rs = [w.bounding_rect for w in words[i:j]]
                    x0, y0 = min(r.x for r in rs), min(r.y for r in rs)
                    x1, y1 = max(r.x + r.width for r in rs), max(r.y + r.height for r in rs)
                    best = ((s, -len(text)), box["left"] + (x0 + x1) / 2 / zoom, box["top"] + (y0 + y1) / 2 / zoom,
                            f"text '{text}'")
    return best[1:] if best else None


def _target_window() -> tuple[int, dict]:
    from ..eyes import active_window_rect

    box = active_window_rect()
    hwnd = box["hwnd"] if box else 0
    if not box:  # whole screen
        box = {"left": 0, "top": 0, "width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1)}
    return hwnd, box


# ── tools ─────────────────────────────────────────────────────
@tool("Click a button, link, icon or text BY WHAT IT SAYS or its name, e.g. 'Play', 'League of Legends', "
      "'Zaloguj', 'New Game'. Finds it exactly (accessibility + reading the screen), much more reliable than "
      "x,y: prefer this whenever the thing has a label.",
      target="the text or name of what to click", double="true for a double click (optional)")
@_guard
def click_on(target: str, double: bool = False) -> dict | str:
    hwnd, box = _target_window()
    found = None
    if hwnd:
        try:
            found = _uia_find(target, hwnd, box)
        except Exception as e:  # noqa: BLE001
            log.debug("ui automation failed: %s", e)
    if not found:
        try:
            found = _ocr_find(target, box)
        except Exception as e:  # noqa: BLE001
            log.warning("ocr failed: %s", e)
    if not found:
        return _after(f"Couldn't find '{target}' on the screen. Look at the screenshot and click it by x,y instead.", 0)
    x, y, what = found
    _check_user()
    _move(x, y)
    time.sleep(0.03)
    for _ in range(2 if str(double).lower() in ("true", "1", "yes") else 1):
        _send(_mouse(MOUSE["left"][0]))
        time.sleep(0.04)
        _send(_mouse(MOUSE["left"][1]))
        time.sleep(0.06)
    return _after(f"Clicked {what}.")
@tool("Click on the screen at x, y (pixels in the latest screenshot, see its grid). Returns a new screenshot.",
      x="x coordinate", y="y coordinate", button='"left" (default), "right" or "middle"',
      double="true for a double click (optional)")
@_guard
def click(x: float, y: float, button: str = "left", double: bool = False) -> dict | str:
    from ..eyes import to_screen

    sx, sy = to_screen(*_in_shot(x, y))
    down, up = MOUSE.get(str(button).lower(), MOUSE["left"])
    _move(sx, sy)
    time.sleep(0.03)
    for _ in range(2 if str(double).lower() in ("true", "1", "yes") else 1):
        _send(_mouse(down))
        time.sleep(0.04)
        _send(_mouse(up))
        time.sleep(0.06)
    return _after(f"Clicked {button} at ({x}, {y}).")


@tool("Move the mouse to x, y (screenshot pixels) without clicking, e.g. to hover or aim.",
      x="x coordinate", y="y coordinate")
@_guard
def move_mouse(x: float, y: float) -> dict | str:
    from ..eyes import to_screen

    _move(*to_screen(*_in_shot(x, y)))
    return _after(f"Mouse at ({x}, {y}).", 0.2)


@tool("Drag with the left mouse button from (x1, y1) to (x2, y2) (screenshot pixels).",
      x1="start x", y1="start y", x2="end x", y2="end y")
@_guard
def drag(x1: float, y1: float, x2: float, y2: float) -> dict | str:
    from ..eyes import to_screen

    ax, ay = to_screen(*_in_shot(x1, y1))
    bx, by = to_screen(*_in_shot(x2, y2))
    _move(ax, ay)
    _send(_mouse(MOUSE["left"][0]))
    for i in range(1, 21):
        _move(ax + (bx - ax) * i / 20, ay + (by - ay) * i / 20)
        time.sleep(0.015)
    _send(_mouse(MOUSE["left"][1]))
    return _after(f"Dragged from ({x1}, {y1}) to ({x2}, {y2}).")


@tool("Scroll the mouse wheel where the mouse is. Positive = up, negative = down.",
      amount="number of notches, e.g. -3")
@_guard
def scroll(amount: float = -3) -> dict | str:
    _send(_mouse(MOUSEEVENTF_WHEEL, int(_num(amount, -3) * 120) & 0xFFFFFFFF))
    return _after(f"Scrolled {amount}.")


@tool("Type text on the keyboard. Give 'into' (the field's name or placeholder, e.g. 'Search', 'Wiadomość') "
      "and it clicks that field first; without it, it types into whatever is focused.",
      text="the text to type", into="the field to click first (optional)")
@_guard
def type_text(text: str, into: str = "") -> dict | str:
    if into and str(into).strip():
        res = click_on.__wrapped__(str(into))  # click the field first (same finder as click_on)
        said = res.get("text", "") if isinstance(res, dict) else str(res)
        if not said.startswith("Clicked"):
            return res  # couldn't find the field: tell the model (it can click by x,y instead)
        time.sleep(0.15)
    for ch in str(text):
        if ch == "\n":
            _send(_key(0x0D, False), _key(0x0D, True))
        else:
            d, u = INPUT(type=INPUT_KEYBOARD), INPUT(type=INPUT_KEYBOARD)
            d.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, 0)
            u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            _send(d, u)
        time.sleep(0.012)
    return _after(f"Typed {len(str(text))} characters.")


@tool("Press keys: one key ('enter', 'space', 'w', 'f1', 'esc'), a combo ('ctrl+c', 'alt+tab'), "
      "or several in a row separated by commas ('ctrl+a, delete'). hold = seconds to hold (for games).",
      keys="the keys", hold="seconds to hold the key down (optional, e.g. 1.5 to walk forward)")
@_guard
def press_keys(keys: str, hold: float = 0) -> dict | str:
    hold = max(0.0, min(10.0, _num(hold)))
    for combo in [c for c in str(keys).split(",") if c.strip()]:
        vks = [_vk(k) for k in combo.replace(" + ", "+").split("+") if k.strip()]
        _send(*[_key(v, False) for v in vks])
        time.sleep(hold or 0.05)
        _send(*[_key(v, True) for v in reversed(vks)])
        time.sleep(0.08)
    return _after(f"Pressed {keys}" + (f" (held {hold:g}s)" if hold else "") + ".")


@tool("Wait a moment (e.g. for a game or page to react) and look at the screen again.", seconds="1 to 10")
@_guard
def wait_and_look(seconds: float = 2) -> dict | str:
    time.sleep(max(0.2, min(10.0, _num(seconds, 2))))
    return _after(f"Waited {seconds}s.", 0)
