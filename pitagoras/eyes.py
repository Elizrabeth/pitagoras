"""Eyes: screenshots for vision-capable brains.

Modes (settings):  off  |  ask (only when the user asks)  |  auto:<minutes> (also glances by itself)
"""
from __future__ import annotations

import base64
import io

from .tools import ctx, tool

MAX_W = 1280
GRID = 100  # grid spacing (screenshot pixels) when Pitagoras controls the PC
GRID_NOTE = (" This shows the active window ({w}x{h} px). Pink grid lines every {g} px; each crossing is labelled "
             "'x,y'. To click something, find the nearest labels and estimate the x,y of its CENTER (aim for the "
             "middle of the button/icon, never its edge). "
             "Then DO it: your next output must be the tool call (click / press_keys / type_text), not a description.")

# where the last screenshot came from, so clicks at screenshot (x, y) land on the right screen pixel
LAST = {"left": 0, "top": 0, "scale": 1.0, "w": 0, "h": 0}


def _our_pids() -> set[int]:
    """This process and everything it started (the character runs in a child process; with a venv there is an
    extra launcher process, so the exe path alone doesn't tell)."""
    import os

    import psutil

    me = psutil.Process(os.getpid())
    pids = {me.pid}
    try:
        pids |= {c.pid for c in me.children(recursive=True)}
        parent = me.parent()  # the venv launcher (python.exe in .venv) that started us
        if parent and "python" in parent.name().lower():
            pids.add(parent.pid)
    except Exception:  # noqa: BLE001
        pass
    return pids


def window_note(limit: int = 60, clicking: bool = True) -> str:
    """The active window's title and the exact names of what can be clicked in it, for the brain.
    A 4K window shrunk to 1280 px is too small to read, so without this it guessed (and invented) titles.
    clicking=False: just to look ("zobacz"), so no click instructions (it clicked things it was only shown)."""
    try:
        from .tools.control import clickable_names

        title, names = clickable_names(limit)
    except Exception:  # noqa: BLE001
        return ""
    if not title and not names:
        return ""
    note = f' Active window: "{title}".'
    if names and not clicking:
        return note + (" What's on it, top to bottom (▶ = videos; just for looking, don't click anything): "
                       + "; ".join(f'"{n}"' for n in names) + ".")
    if names:
        note += (" Things you can click in it, top to bottom. Videos are numbered ▶1, ▶2… (\"drugi film\" = ▶2; "
                 "other entries are channels/buttons). Use these EXACT names with click_on (the ▶number can be left "
                 "out); never use a title that isn't in this list: " + "; ".join(f'"{n}"' for n in names) + ".")
    return note


def is_ours(hwnd) -> bool:
    """One of Pitagoras' own windows (character, bubble, chat box, timer panel)?"""
    import ctypes
    from ctypes import wintypes

    import psutil

    from .config import ROOT

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in _our_pids():
        return True
    try:
        p = psutil.Process(pid.value)
        return (str(p.exe()).lower().startswith(str(ROOT).lower())
                or any("pitagoras.overlay" in a for a in p.cmdline()))
    except Exception:  # noqa: BLE001
        return False


def user_window() -> int:
    """The window you're using: the one in front, or, when that's Pitagoras (you typed into its chat box), the
    next real window below it."""
    import ctypes

    user32 = ctypes.windll.user32
    h = user32.GetForegroundWindow()
    for _ in range(80):  # skip our own windows: take the next window down
        if not h:
            break
        if user32.IsWindowVisible(h) and not user32.IsIconic(h) and not is_ours(h):
            ex = user32.GetWindowLongW(h, -20)  # GWL_EXSTYLE
            cloaked = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
            if not (ex & 0x80) and not cloaked.value:  # not a tool window, not hidden by the shell
                break
        h = user32.GetWindow(h, 2)  # GW_HWNDNEXT
    return h or 0


def active_window_rect() -> dict | None:
    """Screen rectangle of the window you're using (not Pitagoras, not the desktop/taskbar)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    h = user_window()
    if not h:
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return None
    r = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # without shadow
        user32.GetWindowRect(h, ctypes.byref(r))
    w, ht = r.right - r.left, r.bottom - r.top
    if w < 80 or ht < 60:
        return None
    return {"left": r.left, "top": r.top, "width": w, "height": ht, "hwnd": h}


def monitors() -> list[dict]:
    """The screens, in Windows' order: left/top/width/height + primary."""
    import mss

    with mss.mss() as sct:
        mons = sct.monitors[1:] or sct.monitors
    return [{**m, "primary": m["left"] == 0 and m["top"] == 0} for m in mons]


def cursor_pos() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    p = wintypes.POINT()
    return (p.x, p.y) if ctypes.windll.user32.GetCursorPos(ctypes.byref(p)) else None


def screen_point(char_pos: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """A point on the screen the eyes should look at (Eyes > Which screen):
    'mouse' = where your mouse is (default), 'character' = where Pitagoras is, '2' = always screen 2."""
    choice = str(((ctx.cfg or {}).get("eyes") or {}).get("screen", "mouse")) if ctx.cfg is not None else "mouse"
    if choice == "character" and char_pos:
        return char_pos
    if choice.isdigit():
        mons = monitors()
        i = int(choice) - 1
        if 0 <= i < len(mons):
            m = mons[i]
            return m["left"] + m["width"] // 2, m["top"] + m["height"] // 2
    return cursor_pos() or char_pos


def capture(point: tuple[int, int] | None = None, grid: bool = False, region: dict | None = None) -> str:
    """JPEG of the screen picked in Eyes > Which screen (point = the character's position), as base64.
    region: capture just this screen rectangle instead (e.g. the active window, bigger and sharper).
    grid=True draws a labelled coordinate grid (helps a vision model aim its clicks)."""
    import mss
    from PIL import Image

    point = screen_point(point)
    with mss.mss() as sct:
        mons = sct.monitors[1:] or sct.monitors
        mon = mons[0]
        if point:
            x, y = point
            for m in mons:
                if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                    mon = m
                    break
        if region:
            mon = {k: int(region[k]) for k in ("left", "top", "width", "height")}
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    # fit into MAX_W x 900 (small windows are enlarged up to 2x so their buttons are big enough to aim at)
    scale = min(MAX_W / img.width, 900 / img.height, 2.0 if region else 1.0)
    if abs(scale - 1) > 0.01:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    LAST.update(left=mon["left"], top=mon["top"], scale=scale, w=img.width, h=img.height)
    if grid:
        draw_grid(img)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def capture_for_control(point=None) -> str:
    """Screenshot for clicking: the active window (sharper) if there is one, else the whole screen; with grid."""
    return capture(point, grid=True, region=active_window_rect())


def draw_grid(img) -> None:
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arialbd.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(GRID, img.width, GRID):
        d.line([(x, 0), (x, img.height)], fill=(255, 0, 255, 90), width=1)
    for y in range(GRID, img.height, GRID):
        d.line([(0, y), (img.width, y)], fill=(255, 0, 255, 90), width=1)
    # labels at every crossing, on a dark box so they're readable on any background
    for x in range(GRID, img.width, GRID):
        for y in range(GRID, img.height, GRID):
            label = f"{x},{y}"
            box = d.textbbox((x + 3, y + 2), label, font=font)
            d.rectangle(box, fill=(0, 0, 0, 150))
            d.text((x + 3, y + 2), label, fill=(255, 120, 255, 255), font=font)


def to_screen(x: float, y: float) -> tuple[int, int]:
    """Screenshot pixel -> real screen pixel."""
    s = LAST["scale"] or 1.0
    return round(LAST["left"] + x / s), round(LAST["top"] + y / s)


def can_see() -> tuple[bool, str]:
    agent = ctx.extra.get("agent")
    if ctx.extra.get("eyes_mode", "ask") == "off":
        return False, "Your eyes are turned off in the settings (right-click you > Eyes)."
    if not agent or not agent.brain or not agent.brain.vision:
        return False, ("You can't see: the current brain has no vision. A vision model is needed "
                       "(e.g. a local brain with an mmproj file, a VLM in LM Studio, or Claude).")
    return True, ""


@tool("Look at the user's screen (takes a screenshot you can see). Use when they ask about what's on screen, "
      "or before clicking/typing on the PC.",
      focus="what to pay attention to (optional)",
      grid="true to draw a coordinate grid for clicking (optional)")
def look_at_screen(focus: str = "", grid: bool = False) -> dict | str:
    ok, why = can_see()
    if not ok:
        return why
    grid = grid if isinstance(grid, bool) else str(grid).lower() in ("true", "1", "yes")
    img = capture_for_control(ctx.extra.get("char_pos")) if grid else capture(ctx.extra.get("char_pos"))
    note = "Here is the user's screen right now." + (f" Focus on: {focus}" if focus else "")
    if grid:
        note += GRID_NOTE.format(w=LAST["w"], h=LAST["h"], g=GRID)
    return {"text": note + window_note(), "image": img}
