"""Mouse & keyboard control, so Pitagoras can click through things and play simple games
(e.g. FNAF 1: click the door/light buttons, open the camera, …).

Coordinates are pixels in the screenshot Pitagoras sees (look_at_screen with grid=true),
mapped back to the real screen. Every action returns a fresh screenshot so it sees the result.

Safety:
  * Settings > "Let Pitagoras use mouse & keyboard" turns it all off.
  * Press Esc (the banner at the top says so) or move the mouse yourself while it's in control and it
    stops immediately (you took over).
  * Its cursor glides (never teleports) and the overlay draws it in your color, so you can follow along.
  * "Stop talking" / a new message interrupts it, and a session has a step limit (control.max_steps).
"""
from __future__ import annotations

import ctypes
import logging
import math
import random
import re
import threading
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
    # Polish names the brain sometimes uses
    "spacja": 0x20, "spację": 0x20, "spacji": 0x20, "spacebar": 0x20, "escape'a": 0x1B, "strzałka w lewo": 0x25,
    "strzałka w prawo": 0x27, "strzałka w górę": 0x26, "strzałka w dół": 0x28,
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


# ── safety: stop when the user grabs the mouse (or presses Esc) ────────────────
_last_pos: tuple[int, int] | None = None
_last_move = 0.0
_abort = threading.Event()  # set by Esc: every running action stops at its next step


class TookOver(Exception):
    pass


class Escaped(Exception):
    pass


def reset() -> None:
    """New request: forget where we left the mouse last time (you've used it since)."""
    global _last_pos
    _last_pos = None
    _abort.clear()


def _cursor() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _check_user() -> None:
    """Esc pressed -> stop. If the mouse isn't where we left it, the user moved it: stop."""
    if _abort.is_set():
        raise Escaped
    if _last_pos is None or time.monotonic() - _last_move > 120:
        return
    x, y = _cursor()
    if abs(x - _last_pos[0]) + abs(y - _last_pos[1]) > 40:
        raise TookOver


def _set_pos(x: float, y: float) -> None:
    global _last_pos, _last_move
    user32.SetCursorPos(int(round(x)), int(round(y)))
    _last_pos, _last_move = _cursor(), time.monotonic()  # where it really is (a game may clip the cursor)


def _ease(u: float) -> float:
    """Slow start, quick middle, gentle landing (like a hand moving a mouse)."""
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def _move(x: float, y: float) -> None:
    """Glide the cursor to (x, y) along a slight curve instead of jumping there, so you can follow it.
    Stops right away if you grab the mouse or press Esc."""
    _check_user()
    sx, sy = _cursor()
    dx, dy = x - sx, y - sy
    dist = math.hypot(dx, dy)
    if dist < 3:
        _set_pos(x, y)
        return
    speed = _num(((ctx.cfg or {}).get("control") or {}).get("cursor_speed", 1.0), 1.0) or 1.0
    dur = min(0.75, max(0.16, 0.12 + dist / 2600)) / max(0.25, speed)
    bend = random.uniform(-0.12, 0.12) * dist  # a little arc, never a ruler-straight line
    cx, cy = sx + dx / 2 - dy / dist * bend, sy + dy / 2 + dx / dist * bend
    t0 = time.perf_counter()
    while True:
        u = min(1.0, (time.perf_counter() - t0) / dur)
        e = _ease(u)
        px = (1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e ** 2 * x
        py = (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e ** 2 * y
        _check_user()
        _set_pos(px, py)
        if u >= 1.0:
            break
        time.sleep(0.007)
    _set_pos(x, y)


def _press(down: int, up: int, double: bool = False, button: str = "left") -> None:
    """Click where the cursor is: a short hover first (menus/hover effects react), then a real-length press."""
    time.sleep(0.07)
    _check_user()
    _emit("control_click", button=button, double=bool(double))
    for i in range(2 if double else 1):
        _send(_mouse(down))
        time.sleep(random.uniform(0.045, 0.075))
        _send(_mouse(up))
        time.sleep(0.09 if double and i == 0 else 0.05)


def _emit(type_: str, **data) -> None:
    if ctx.hub is not None:
        ctx.hub.emit(type_, **data)


def _label(text: str) -> None:
    """A short note next to Pitagoras's cursor ('Ctrl+C', 'typing: hello…')."""
    _emit("control_label", text=text)


# ── control mode: the on-screen frame + "press Esc" banner, and the Esc key ────────────────
_mode = {"on": False, "thread": None, "tid": 0, "inject_until": 0.0}
LLKHF_INJECTED, WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT, VK_ESCAPE = 0x10, 0x100, 0x104, 0x12, 0x1B


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK


def active() -> bool:
    return _mode["on"]


def _escape_pressed() -> None:
    if _abort.is_set() or not _mode["on"]:
        return
    log.info("Esc: the user stopped PC control")
    _abort.set()
    ctx.extra["control_stopped"] = True
    agent = ctx.extra.get("agent")
    hub = ctx.hub
    if agent is not None and hub is not None and hub.loop is not None:
        hub.loop.call_soon_threadsafe(agent.stop_control)


def _esc_watch() -> None:
    """Own thread: a keyboard hook that catches YOUR Esc (and keeps it from the app underneath);
    Pitagoras's own key presses are 'injected' and pass through. Polling the key is a backup."""
    _mode["tid"] = ctypes.windll.kernel32.GetCurrentThreadId()

    def proc(code, wparam, lparam):
        if code == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYDOWN + 1, WM_SYSKEYDOWN + 1):
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if k.vkCode == VK_ESCAPE and not k.flags & LLKHF_INJECTED and _mode["on"]:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    _escape_pressed()
                return 1  # swallowed: the game/app doesn't also get an Esc
        return user32.CallNextHookEx(None, code, wparam, lparam)

    cb = _HOOKPROC(proc)
    hook = user32.SetWindowsHookExW(13, cb, None, 0)  # WH_KEYBOARD_LL
    msg = wintypes.MSG()
    try:
        while _mode["on"]:
            # pump messages (the hook needs it) and poll Esc as a backup, ~30 times a second
            user32.MsgWaitForMultipleObjects(0, None, False, 30, 0x04FF)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_QUIT:
                    return
            if (user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000 and time.monotonic() > _mode["inject_until"]):
                _escape_pressed()
    finally:
        if hook:
            user32.UnhookWindowsHookEx(hook)
        del cb


def begin(text: str = "") -> None:
    """Pitagoras may click and type now: show its cursor, the frame and the 'press Esc' banner."""
    if _mode["on"]:
        return
    _abort.clear()
    _mode["on"] = True
    _mode["thread"] = threading.Thread(target=_esc_watch, daemon=True, name="esc-watch")
    _mode["thread"].start()
    _emit("control", on=True, text=text or "Naciśnij Esc, aby wyjść z trybu kontroli")


def end() -> None:
    if not _mode["on"]:
        return
    _mode["on"] = False
    if _mode["tid"]:
        user32.PostThreadMessageW(_mode["tid"], WM_QUIT, 0, 0)
    _mode["tid"] = 0
    _emit("control", on=False)


def _allowed() -> str | None:
    c = (ctx.cfg or {}).get("control") or {}
    if not c.get("enabled", True):
        return "Mouse & keyboard control is turned off (right-click Pitagoras > Settings)."
    return None


def _after(text: str, wait: float = 0.35) -> dict | str:
    """Result + a fresh screenshot (with grid) so the model sees what happened."""
    time.sleep(wait)
    from ..eyes import GRID, GRID_NOTE, LAST, can_see, capture_for_control

    from ..eyes import window_note

    if not can_see()[0]:
        return text + window_note()
    img = capture_for_control(ctx.extra.get("char_pos"))
    return {"text": text + " Here is the screen now." + GRID_NOTE.format(w=LAST["w"], h=LAST["h"], g=GRID)
            + window_note(), "image": img}


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
        except Escaped:
            ctx.extra["control_stopped"] = True
            return "STOPPED: the user pressed Esc to end control mode. Stop controlling the PC now."
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


def _score(want: str, have: str) -> float:
    """How well a label on screen matches what we want to click (0 = no). A short label that is only a piece of
    a long target ('Adam' for 'Adam Show - odc. 9') does NOT count: that clicked wrong things."""
    import difflib

    if not want or not have:
        return 0.0
    if have == want:
        return 1.0
    if want in have:  # the whole target inside a longer label (title + duration, "Play X")
        return 0.75 + 0.2 * len(want) / len(have)
    if have in want:  # the label is part of the target: only if it covers most of it
        cover = len(have) / len(want)
        return 0.2 + 0.6 * cover if cover >= 0.6 else 0.0
    r = difflib.SequenceMatcher(None, want, have).ratio()
    return r * 0.8 if r >= 0.8 else 0.0


# UI Automation control types: what can be clicked, and plain text (for finding labels)
UIA_CLICKABLE = {50005: "Hyperlink", 50000: "Button", 50007: "ListItem", 50011: "MenuItem", 50019: "TabItem",
                 50013: "RadioButton", 50002: "CheckBox", 50031: "SplitButton", 50003: "ComboBox", 50024: "TreeItem",
                 50029: "DataItem"}
UIA_TEXT = {50020: "Text", 50006: "Image"}
WINDOW_CHROME = {"minimize", "maximize", "restore", "close", "system", "minimalizuj", "maksymalizuj", "przywróć",
                 "zamknij", "go back", "go forward", "back", "forward", "reload", "wstecz", "dalej", "odśwież"}
_UIA: dict = {}


def _uia_conds():
    if not _UIA:
        from pywinauto.uia_defines import IUIA

        iu = IUIA().iuia
        onscreen = iu.CreatePropertyCondition(30022, False)  # IsOffscreen = False

        def of(types):
            return iu.CreateAndCondition(iu.CreateOrConditionFromArray(
                [iu.CreatePropertyCondition(30003, t) for t in types]), onscreen)

        _UIA.update(iu=iu, click=of(UIA_CLICKABLE), all=of({**UIA_CLICKABLE, **UIA_TEXT}))
    return _UIA


def _clean_label(name: str) -> str:
    name = re.sub(r"^[^\w\"'(]+", "", name.strip())  # "█▬█ █ ▀█▀ Adam…" -> "Adam…"
    return re.sub(r"\s+", " ", name)[:110]


def screen_items(hwnd: int, box: dict, text_too: bool = False, limit: int = 0, page_only: bool = False) -> list[dict]:
    """The things in a window you can click (links, buttons, list items, tabs…), as the window itself names them,
    visible and on screen, top to bottom. Fast (one UI Automation query, ~0.1 s even for a YouTube page)."""
    if not hwnd:
        return []
    u = _uia_conds()
    types = {**UIA_CLICKABLE, **UIA_TEXT}
    root = u["iu"].ElementFromHandle(hwnd)
    if page_only:  # a browser: just the web page, not the tabs/toolbar ("New Tab", "Bookmark this tab"…)
        doc = root.FindFirst(4, u["iu"].CreatePropertyCondition(30003, 50030))  # Document
        if doc:
            root = doc
    arr = root.FindAll(4, u["all" if text_too else "click"])  # 4 = descendants
    items, seen = [], set()
    x0, y0, x1, y1 = box["left"], box["top"], box["left"] + box["width"], box["top"] + box["height"]
    for i in range(arr.Length):
        try:
            e = arr.GetElement(i)
            name = _clean_label(e.CurrentName or "")
            r = e.CurrentBoundingRectangle
            if len(name) < 2 or r.right - r.left < 4 or r.bottom - r.top < 4:
                continue
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if not (x0 <= cx <= x1 and y0 <= cy <= y1) or name.lower() in WINDOW_CHROME:
                continue
            kind = types.get(e.CurrentControlType, "")
            if kind in UIA_TEXT.values() and len(name) < 3:
                continue
            key = (name.lower(), kind in UIA_CLICKABLE.values())
            if key in seen:
                continue
            seen.add(key)
            items.append({"name": name, "kind": kind, "x": cx, "y": cy, "top": r.top, "left": r.left,
                          "clickable": kind in UIA_CLICKABLE.values()})
        except Exception:  # noqa: BLE001
            continue
    items.sort(key=lambda it: (it["top"] // 20, it["left"]))
    return items[:limit] if limit else items


def _uia_find(target: str, hwnd: int, box: dict) -> tuple[int, int, str] | None:
    """A control in the window whose accessible name matches (buttons, links, tabs, icons…)."""
    want = _fold(target)
    best = None
    for it in screen_items(hwnd, box, text_too=True):
        s = _score(want, _fold(it["name"]))
        if s < 0.5:
            continue
        rank = (round(s, 3), it["clickable"], -len(it["name"]))
        if best is None or rank > best[0]:
            best = (rank, it["x"], it["y"], f"{it['kind']} '{it['name']}'")
    return best[1:] if best else None


def clickable_names(limit: int = 60) -> tuple[str, list[str]]:
    """(window title, names of what can be clicked in the active window) for the brain: it picks from these
    instead of reading tiny text off a scaled-down screenshot (and inventing titles)."""
    hwnd, box = _target_window()
    if not hwnd:
        return "", []
    buf = ctypes.create_unicode_buffer(300)
    user32.GetWindowTextW(hwnd, buf, 300)
    try:
        items = [it for it in screen_items(hwnd, box, page_only=True) if it["clickable"]]
    except Exception as e:  # noqa: BLE001
        log.debug("listing clickable things failed: %s", e)
        items = []
    n = 0
    out = []
    for it in items[:limit]:  # videos (they have a length) are numbered, so "drugi film" = ▶2
        if VIDEO_LEN_RE.search(it["name"]):
            n += 1
            out.append(f"▶{n} {it['name']}")
        else:
            out.append(it["name"])
    return buf.value, out


VIDEO_LEN_RE = re.compile(r"\b\d+ (?:minut\w*|sekund\w*|godzin\w*|minutes?|seconds?|hours?)\b|\b\d{1,2}:\d{2}\b", re.I)


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
                if s >= 0.5 and (best is None or (s, -len(text)) > best[0]):
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
    target = re.sub(r"^\s*▶\d*\s*", "", str(target)).strip()
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
        return _after(f"Couldn't find '{target}' on the screen (it isn't there, or it's named differently). "
                      "Pick one of the exact names listed below, or click by x,y from the screenshot.", 0)
    x, y, what = found
    _label(what.split("'")[1][:40] if "'" in what else "")
    _move(x, y)
    _press(*MOUSE["left"], double=_yes(double))
    return _after(f"Clicked {what}.")


def _yes(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


@tool("Click on the screen at x, y (pixels in the latest screenshot, see its grid). Returns a new screenshot.",
      x="x coordinate", y="y coordinate", button='"left" (default), "right" or "middle"',
      double="true for a double click (optional)")
@_guard
def click(x: float, y: float, button: str = "left", double: bool = False) -> dict | str:
    from ..eyes import to_screen

    sx, sy = to_screen(*_in_shot(x, y))
    button = str(button).lower() if str(button).lower() in MOUSE else "left"
    _move(sx, sy)
    _press(*MOUSE[button], double=_yes(double), button=button)
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
    time.sleep(0.08)
    _label("✊")
    _send(_mouse(MOUSE["left"][0]))
    try:
        time.sleep(0.1)
        _move(bx, by)
        time.sleep(0.08)
    finally:
        _send(_mouse(MOUSE["left"][1]))  # never leave the button held down
    return _after(f"Dragged from ({x1}, {y1}) to ({x2}, {y2}).")


@tool("Scroll the mouse wheel. Positive = up, negative = down. Give x, y to scroll over a spot "
      "(e.g. a list or a page); without them it scrolls where the mouse is.",
      amount="number of notches, e.g. -3", x="where to scroll, x (optional)", y="where to scroll, y (optional)")
@_guard
def scroll(amount: float = -3, x: float | None = None, y: float | None = None) -> dict | str:
    if x not in (None, "") and y not in (None, ""):
        from ..eyes import to_screen

        _move(*to_screen(*_in_shot(x, y)))
    n = int(round(_num(amount, -3))) or -1
    n = max(-30, min(30, n))
    _label("↑" if n > 0 else "↓")
    for _ in range(abs(n)):  # notch by notch, smooth like a real wheel
        _check_user()
        _send(_mouse(MOUSEEVENTF_WHEEL, (120 if n > 0 else -120) & 0xFFFFFFFF))
        time.sleep(0.045)
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
    text = str(text)
    # visible, human-ish speed for short text; long text goes quicker so it doesn't take ages
    base = 0.045 if len(text) <= 40 else 0.022 if len(text) <= 200 else 0.006
    shown = ""
    for i, ch in enumerate(text):
        _check_user()
        if ch == "\n":
            _send(_key(0x0D, False), _key(0x0D, True))
        else:
            d, u = INPUT(type=INPUT_KEYBOARD), INPUT(type=INPUT_KEYBOARD)
            d.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, 0)
            u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            _send(d, u)
        shown = (shown + ("⏎" if ch == "\n" else ch))[-28:]
        if i % 2 == 0 or i == len(text) - 1:
            _label("⌨ " + shown)
        time.sleep(base * random.uniform(0.6, 1.5) + (base * 0.8 if ch in " .,!?" else 0))
    return _after(f"Typed {len(text)} characters.")


KEY_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "windows": "Win",
             "enter": "Enter", "return": "Enter", "esc": "Esc", "escape": "Esc", "space": "Space", "tab": "Tab",
             "backspace": "⌫", "delete": "Del", "del": "Del", "up": "↑", "down": "↓", "left": "←", "right": "→"}


@tool("Press keys: one key ('enter', 'space', 'w', 'f1', 'esc'), a combo ('ctrl+c', 'alt+tab'), "
      "or several in a row separated by commas ('ctrl+a, delete'). hold = seconds to hold (for games).",
      keys="the keys", hold="seconds to hold the key down (optional, e.g. 1.5 to walk forward)")
@_guard
def press_keys(keys: str, hold: float = 0) -> dict | str:
    hold = max(0.0, min(10.0, _num(hold)))
    for combo in [c for c in str(keys).split(",") if c.strip()]:
        _check_user()
        names = [k.strip() for k in combo.replace(" + ", "+").split("+") if k.strip()]
        vks = [_vk(k) for k in names]
        if VK_ESCAPE in vks:  # our own Esc must not count as "the user pressed Esc"
            _mode["inject_until"] = time.monotonic() + hold + 0.6
        _label("⌨ " + "+".join(KEY_NAMES.get(n.lower(), n.upper() if len(n) <= 3 else n.title()) for n in names))
        _send(*[_key(v, False) for v in vks])
        try:
            end_at = time.monotonic() + (hold or 0.06)
            while time.monotonic() < end_at:  # a long hold can still be stopped with Esc
                if _abort.is_set():
                    break
                time.sleep(0.02)
        finally:
            _send(*[_key(v, True) for v in reversed(vks)])  # never leave a key stuck down
        time.sleep(0.09)
    return _after(f"Pressed {keys}" + (f" (held {hold:g}s)" if hold else "") + ".")


@tool("Wait a moment (e.g. for a game or page to react) and look at the screen again.", seconds="1 to 10")
@_guard
def wait_and_look(seconds: float = 2) -> dict | str:
    time.sleep(max(0.2, min(10.0, _num(seconds, 2))))
    return _after(f"Waited {seconds}s.", 0)
