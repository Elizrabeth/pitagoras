"""Write on Discord for you: "napisz na discordzie na serwerze X na kanale Y: będę za 10 minut".

It drives the Discord desktop app like a person would, but checks every step:
  1. Discord to the front (started if it isn't running).
  2. Ctrl+K (quick switcher) -> the channel, picking the result that is on the right server.
  3. The window title must now say "#channel | Server": otherwise it stops and sends NOTHING.
  4. Clears any draft, types the message + the signature ("- Pitagoras AI"), Enter.
  5. Checks the message really showed up in the channel, then puts your previous window back (e.g. a game).
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import time
from ctypes import wintypes

from . import ctx, tool
from . import control as c

log = logging.getLogger("discord")
user32 = ctypes.windll.user32

CT_EDIT, CT_LISTITEM = 50004, 50007
SIGNATURE = "- Pitagoras AI"


class Fail(Exception):
    pass


# ── window ────────────────────────────────────────────────────
def _windows() -> list[tuple[int, str, bool, int]]:
    """(hwnd, title, visible, area) of Discord's main windows."""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _):
        b = ctypes.create_unicode_buffer(400)
        user32.GetWindowTextW(h, b, 400)
        t = b.value
        if t == "Discord" or t.endswith(" - Discord"):
            cls = ctypes.create_unicode_buffer(100)
            user32.GetClassNameW(h, cls, 100)
            if cls.value == "Chrome_WidgetWin_1":
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                out.append((h, t, bool(user32.IsWindowVisible(h)), (r.right - r.left) * (r.bottom - r.top)))
        return True

    user32.EnumWindows(cb, 0)
    return sorted(out, key=lambda w: (w[2], w[1] != "Discord", w[3]), reverse=True)


def _title(h: int) -> str:
    b = ctypes.create_unicode_buffer(400)
    user32.GetWindowTextW(h, b, 400)
    return b.value


def _start_discord() -> None:
    upd = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Discord", "Update.exe")
    if os.path.exists(upd):
        subprocess.Popen([upd, "--processStart", "Discord.exe"])
    else:
        os.startfile("discord://")


def _window() -> int:
    """Discord's main window, in front and ready (starts Discord / brings it out of the tray)."""
    wins = [w for w in _windows() if w[2] and w[3] > 200 * 200]
    if not wins:
        _start_discord()  # not running, or hidden in the tray: this opens / shows it
        for _ in range(60):
            time.sleep(0.5)
            wins = [w for w in _windows() if w[2] and w[3] > 200 * 200 and w[1] != "Discord"]
            if wins:
                time.sleep(1.5)  # let it finish loading
                break
        else:
            raise Fail("Discord didn't open (is it installed?).")
    h = wins[0][0]
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)  # SW_RESTORE
    for _ in range(3):
        c._send(c._key(0x12, False), c._key(0x12, True))  # (a tap of Alt lets us take the foreground)
        user32.SetForegroundWindow(h)
        time.sleep(0.25)
        if user32.GetForegroundWindow() == h:
            return h
    raise Fail("couldn't bring Discord to the front.")


# ── keyboard ──────────────────────────────────────────────────
def _keys(*vks: int) -> None:
    c._check_user()
    c._send(*[c._key(v, False) for v in vks])
    time.sleep(0.05)
    c._send(*[c._key(v, True) for v in reversed(vks)])
    time.sleep(0.08)


def _esc() -> None:
    c._mode["inject_until"] = time.monotonic() + 0.6  # (our Esc, not the user's "stop")
    _keys(0x1B)


def _type(text: str, label: bool = True) -> None:
    shown = ""
    for ch in text:
        c._check_user()
        if ch == "\n":  # a new line inside the message (Enter alone would send it)
            _keys(0x10, 0x0D)
        else:
            d, u = c.INPUT(type=c.INPUT_KEYBOARD), c.INPUT(type=c.INPUT_KEYBOARD)
            d.ki = c.KEYBDINPUT(0, ord(ch), c.KEYEVENTF_UNICODE, 0, 0)
            u.ki = c.KEYBDINPUT(0, ord(ch), c.KEYEVENTF_UNICODE | c.KEYEVENTF_KEYUP, 0, 0)
            c._send(d, u)
        if label:
            shown = (shown + ch)[-28:]
            c._label("⌨ " + shown)
        time.sleep(0.018)


# ── reading Discord (UI Automation) ───────────────────────────
def _elements(h: int, ctype: int) -> list:
    iu = c._uia_conds()["iu"]
    root = iu.ElementFromHandle(h)
    arr = root.FindAll(4, iu.CreatePropertyCondition(30003, ctype))
    return [arr.GetElement(i) for i in range(arr.Length)]


def _switcher_items(h: int) -> list[tuple[str, object]]:
    out = []
    for e in _elements(h, CT_LISTITEM):
        try:
            if "quick-switcher" in (e.CurrentAutomationId or ""):
                out.append((e.CurrentName or "", e))
        except Exception:  # noqa: BLE001
            continue
    return out


def _fold(s: str) -> str:
    return c._fold(s)


def _ascii(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s.replace("ł", "l").replace("Ł", "L"))
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _match(want: str, have: str) -> float:
    w, hv = _fold(want), _fold(have)
    if not w or not hv:
        return 0.0
    if w == hv:
        return 1.0
    return c._score(w, hv)


def _switcher_open(h: int) -> bool:
    try:
        return any((e.CurrentName or "") == "Quick Switcher" or "quick-switcher" in (e.CurrentAutomationId or "")
                   for e in _elements(h, 50003))  # its search box (a combo box)
    except Exception:  # noqa: BLE001
        return False


def _open_switcher(h: int) -> None:
    for _ in range(4):
        _keys(0x11, 0x4B)  # Ctrl+K
        for _ in range(8):
            time.sleep(0.1)
            if _switcher_open(h):
                time.sleep(0.15)
                return
        time.sleep(0.3)  # (right after closing it, Discord ignores Ctrl+K for a moment)
    raise Fail("Discord's quick switcher (Ctrl+K) didn't open.")


def _pick(h: int, query: str | list[str], name: str | list[str], server: str) -> str | None:
    """Quick switcher: search (each query in turn, retyped in place), then choose the result for this channel
    (on this server). Returns the name that matched (switched there), or None."""
    queries = [query] if isinstance(query, str) else query
    names = [name] * len(queries) if isinstance(name, str) else name
    _open_switcher(h)
    for q, n in zip(queries, names):
        _keys(0x11, 0x41)  # Ctrl+A: the next query replaces the last one
        _type(q, label=False)
        if _choose(h, n, server):
            return n
    _esc()
    time.sleep(0.4)
    return None


def _choose(h: int, name: str, server: str) -> bool:
    best = None
    last = None
    for _ in range(12):  # results come in as you type
        time.sleep(0.25)
        items = _switcher_items(h)
        labels = [lb for lb, _e in items]
        if labels == last and (labels or _ > 4):
            break  # settled, and nothing on it fits
        last = labels
        best = None
        for i, (label, _e) in enumerate(items):
            parts = [p.strip() for p in label.split(",")]
            chan, place = parts[0], parts[-1] if len(parts) > 1 else ""
            s = _match(name, chan) * 2 + (_match(server, place) * 1.5 if server else 0) - i * 0.01
            ok = _match(name, chan) >= 0.75 and (not server or _match(server, place) >= 0.6)
            if ok and (best is None or s > best[0]):
                best = (s, i, label)
        if best:
            break
    if not best:
        return False
    log.info("quick switcher: %r", best[2])
    for _ in range(best[1]):  # the first result is already selected
        _keys(0x28)
        time.sleep(0.03)
    _keys(0x0D)
    time.sleep(0.7)
    return True


def _go_to(h: int, server: str, channel: str, dm: str) -> None:
    if dm:
        # "do Adama" -> also try "Adam", "Ada" (Polish endings)
        tries = [dm] + [dm[:-k] for k in (1, 2) if len(dm) - k >= 3]
        who = _pick(h, ["@" + t for t in tries], tries, "")
        if not who:
            raise Fail(f"couldn't find '{dm}' in your Discord friends / DMs.")
        if _fold(who) not in _fold(_title(h)):
            raise Fail(f"opened the wrong chat ({_title(h)!r}), so I sent nothing.")
        return f"to {who}"
    channel = channel.strip().lstrip("#")
    _open_switcher(h)
    full = _resolve_server(h, server) if server else ""
    found = _find_channel(h, channel, full)
    if found is None and full:  # many channels with that name: go to the server first, its own come first then
        res = _search(h, "*" + full)
        i = next((i for i, r in enumerate(res) if r[1] == "Server" and _fold(r[0]) == _fold(full)), None)
        if i is not None:
            _select(i)
            time.sleep(0.6)
            _open_switcher(h)
            found = _find_channel(h, channel, full)
    if found is None:
        _esc()
        where = f" on '{full}'" if full else ""
        raise Fail(f"couldn't find a text channel '{channel}'{where}.")
    i, chan, place = found
    log.info("quick switcher: #%s on %s", chan, place)
    _select(i)
    # the window must now say exactly that channel on exactly that server, or nothing gets sent
    t = _title(h)  # "#general | moja ekipa - Discord"
    m = re.match(r"#?(.*?) \| (.*) - Discord$", t)
    if not (m and _fold(m.group(1)) == _fold(chan) and _fold(m.group(2)) == _fold(place)):
        raise Fail(f"Discord shows {t!r}, not #{chan} on {place}, so I sent nothing.")
    return f"in #{chan} on {place}"


SWITCHER_MAX = 15  # Discord lists at most about this many results


def _results(h: int) -> list[tuple[str, str, str]]:
    """The quick switcher's results once they stop changing: (name, kind, server).
    'general, Text Channel, moja ekipa' / 'moja ekipa, Server'."""
    last = None
    for n in range(14):
        time.sleep(0.25)
        labels = [lb for lb, _e in _switcher_items(h)]
        if labels == last and (labels or n > 4):
            break
        last = labels
    out = []
    for lb in last or []:
        if lb.endswith(", Server"):
            out.append((lb[: -len(", Server")].strip(), "Server", ""))
        else:
            parts = [p.strip() for p in lb.split(",", 2)]
            out.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    return out


def _search(h: int, q: str) -> list[tuple[str, str, str]]:
    _keys(0x11, 0x41)  # Ctrl+A: replaces the previous search
    _type(q, label=False)
    return _results(h)


def _select(i: int) -> None:
    for _ in range(i):  # the first result is already selected
        _keys(0x28)
        time.sleep(0.03)
    _keys(0x0D)
    time.sleep(0.7)


def _ask(names: list[str]) -> str:
    shown = ", ".join(f"'{n}'" for n in names[:6]) + (" …" if len(names) > 6 else "")
    return f"{shown}. Nothing was sent: ask the user which one they mean."


def _resolve_server(h: int, server: str) -> str:
    """The exact name of the server the user means. A shortened name is fine only if exactly ONE of their servers
    fits it; if several fit, or none, it stops (and the user is asked) instead of guessing."""
    res: list = []
    for q in dict.fromkeys(["*" + server, "*" + _ascii(server)]):
        res = [r for r in _search(h, q) if r[1] == "Server"]
        if res:
            break
    # Discord's server search leaves out the server you're on right now: that one comes from the window title
    cur = re.match(r".*? \| (.*) - Discord$", _title(h))
    names = list(dict.fromkeys(r[0] for r in res))
    if cur and _match(server, cur.group(1)) >= 0.6 and cur.group(1) not in names:
        names.insert(0, cur.group(1))
    exact = [n for n in names if _fold(n) == _fold(server)]
    if exact:
        return exact[0]
    fits = sorted((n for n in names if _match(server, n) >= 0.6), key=lambda n: -_match(server, n))
    if len(fits) == 1:
        return fits[0]
    _esc()
    if not fits:
        raise Fail(f"you aren't on a Discord server called '{server}'"
                   + (f" (closest: {', '.join(names[:3])})" if names else "") + ". Nothing was sent.")
    raise Fail(f"'{server}' fits several of your servers: " + _ask(fits))


def _find_channel(h: int, channel: str, server: str) -> tuple[int, str, str] | None:
    """(result index, channel, server) of THE text channel meant. Without a server it must be the only
    channel with that name among your servers, else it stops and asks."""
    q = re.sub(r"\s+", "-", channel.lower())
    plain = _ascii(q)  # "ogłoszenia" -> "ogloszenia" (how channels are usually named)
    for query in dict.fromkeys(["#" + q, "#" + plain]):
        res = _search(h, query)
        rows = [(i, r) for i, r in enumerate(res) if r[1] != "Server" and r[2]]
        if server:
            rows = [(i, r) for i, r in rows if _fold(r[2]) == _fold(server)]
        exact = [(i, r) for i, r in rows if _fold(r[0]) == _fold(channel)]
        close = exact or [(i, r) for i, r in rows if _match(channel, r[0]) >= 0.75]
        if not close:
            continue
        if server:
            if len({_fold(r[0]) for _i, r in close}) > 1:  # "gen" fits #general and #generator
                _esc()
                raise Fail(f"'{channel}' fits several channels on '{server}': " + _ask([r[0] for _i, r in close]))
            i, r = close[0]
            return i, r[0], r[2]
        places = list(dict.fromkeys(r[2] for _i, r in close))
        # a full list that still ends in matches may hide more of them below: then it's not sure either
        cut_off = len(res) >= SWITCHER_MAX and any(i == len(res) - 1 for i, _r in close)
        if len(places) > 1 or cut_off:
            _esc()
            raise Fail(f"there's a #{close[0][1][0]} on several of your servers: " + _ask(places))
        i, r = close[0]
        if len({_fold(x[0]) for _j, x in close}) > 1:
            _esc()
            raise Fail(f"'{channel}' fits several channels: " + _ask([x[0] for _j, x in close]))
        return i, r[0], r[2]
    return None


def _composer(h: int):
    best = None
    for e in _elements(h, CT_EDIT):
        try:
            n = e.CurrentName or ""
            r = e.CurrentBoundingRectangle
            if r.right - r.left < 50 or e.CurrentIsOffscreen:
                continue
            score = (n.startswith(("Message", "Wiadomość", "Wyślij")) or "#" in n or "@" in n, r.bottom)
            if best is None or score > best[0]:
                best = (score, e, r)
        except Exception:  # noqa: BLE001
            continue
    if not best:
        raise Fail("there's no message box here (no permission to write, or the channel is read-only).")
    return best[1], best[2]


def _composer_text(e) -> str:
    try:
        return str(e.GetCurrentPropertyValue(30045) or "")  # ValueValue
    except Exception:  # noqa: BLE001
        return ""


def _last_messages(h: int, n: int = 4) -> list[str]:
    msgs = []
    for e in _elements(h, CT_LISTITEM):
        try:
            if (e.CurrentAutomationId or "").startswith("chat-messages"):
                msgs.append(e.CurrentName or "")
        except Exception:  # noqa: BLE001
            continue
    return msgs[-n:]


def _restore(prev: int, h: int) -> None:
    if prev and prev != h and user32.IsWindow(prev):
        c._send(c._key(0x12, False), c._key(0x12, True))
        user32.SetForegroundWindow(prev)


# ── understanding the request (from the user's own words) ─────────
QUOTE_RE = re.compile(r"[\"„“”«»]([^\"„“”«»]+)[\"„“”«»]")
MSG_START_RE = re.compile(r"(?:\s*:\s*|\s+(?:że|ze|żeby|zeby|treść|tresc|wiadomość|wiadomosc|text|saying|that)\s+)", re.I)
SERVER_RE = re.compile(r"\b(?:na\s+|w\s+|on\s+(?:the\s+)?)?(?:serwer\w*|server)\s+(.+)", re.I)
CHANNEL_RE = re.compile(r"\b(?:na\s+|w\s+|in\s+|on\s+)?(?:kana\w*|channel)\s+(.+)|(?:^|\s)#(\S+)", re.I)
STOP_RE = re.compile(r"\s+(?:na|w|on|in)\s+(?:serwer\w*|server|kana\w*|channel|discord\w*|dc)\b|\s*,\s*|\s*$", re.I)


def _until_stop(s: str) -> str:
    m = STOP_RE.search(s)
    return s[: m.start()].strip(" #\"'„”") if m else s.strip()


def parse_request(text: str) -> dict:
    """{'server', 'channel', 'message'} that are CLEAR in the user's words ('' when unsure: the brain decides).
    Handles: '"serwer, kanał" "wiadomość"', 'na serwerze X na kanale Y: wiadomość', '… na kanale Y, że wiadomość'."""
    out = {"server": "", "channel": "", "message": "", "to": ""}
    text = re.sub(r"\s+", " ", text).strip()
    quotes = [q.strip() for q in QUOTE_RE.findall(text)
              if q.strip() and not re.fullmatch(r"[\s\-–—]*pitagoras(?: ai)?", q.strip(), re.I)]
    if quotes:
        where = next((q for q in quotes[:-1] if "," in q), "") if len(quotes) > 1 else ""
        if where:
            srv, chan = where.rsplit(",", 1)
            out["server"], out["channel"] = srv.strip(), chan.strip().lstrip("#")
        out["message"] = quotes[-1]
        text = QUOTE_RE.sub(" ", text)
    else:
        m = None
        for m_ in MSG_START_RE.finditer(text):  # the message starts after ':' / 'że' that follows where to send it
            if CHANNEL_RE.search(text[: m_.start()]) or re.search(r"\b(?:do|to)\s+\w+", text[: m_.start()]):
                m = m_
                break
        if m:
            out["message"] = text[m.end():].strip().strip("\"'„”")
            text = text[: m.start()]
            dm = re.search(r"\b(?:do|to)\s+(\w+)\s*$", text)
            if dm and not CHANNEL_RE.search(text) and dm.group(1).lower() not in ("discorda", "discord", "dc"):
                out["to"] = dm.group(1)
        else:
            text = ""  # no clear border between the channel and the message: leave it all to the brain
    if text and not out["server"]:
        s = SERVER_RE.search(text)
        if s:
            out["server"] = _until_stop(s.group(1))
    if text and not out["channel"]:
        ch = CHANNEL_RE.search(text)
        if ch:
            out["channel"] = (_until_stop(ch.group(1)) if ch.group(1) else ch.group(2)).lstrip("#")
    out["message"] = re.sub(r"[\s\-–—]*pitagoras ai\s*$", "", out["message"], flags=re.I).strip()
    return out


# ── the tool ──────────────────────────────────────────────────
def signed(message: str) -> str:
    sig = str(((ctx.cfg or {}).get("discord") or {}).get("signature", SIGNATURE) if ctx.cfg is not None else SIGNATURE)
    msg = str(message).strip().strip('"„”“').strip()
    if sig.strip():
        core = re.sub(r"[\s\-–—]*pitagoras(?: ai)?\s*$", "", msg, flags=re.I).rstrip() or msg  # signed only once
        msg = f"{core} {sig.strip()}"
    return msg


@tool("Send a message on Discord for the user (it gets signed '- Pitagoras AI'). Give the server and the text "
      "channel, or 'to' for a direct message to a person. Write the message EXACTLY as the user said it.",
      message="the exact text to send", server="the Discord server's name", channel="the text channel's name",
      to="a person's name, for a direct message instead of a channel (optional)")
def send_discord(message: str, server: str = "", channel: str = "", to: str = "") -> str:
    why = c._allowed()
    if why:
        return why
    message, server, channel, to = (str(x or "").strip() for x in (message, server, channel, to))
    if not message:
        return "Error: what should I write? (no message given)"
    if not channel and not to:
        return "Error: which channel (and server), or which person? Ask the user."
    if channel:
        to = ""  # a channel was named: that's where it goes
    text = signed(message)
    agent = ctx.extra.get("agent")
    own_mode = not c.active()
    if own_mode:
        c.reset()
        c.begin(agent.control_banner() if agent else "")
    prev = user32.GetForegroundWindow()
    h = 0
    typed_in = False
    try:
        h = _window()
        c._label("Discord")
        where = _go_to(h, server, channel, to)
        box, r = _composer(h)
        c._move((r.left + r.right) / 2, (r.top + r.bottom) / 2)  # glide to the message box and click it
        c._press(*c.MOUSE["left"])
        time.sleep(0.15)
        if user32.GetForegroundWindow() != h:
            raise Fail("Discord lost focus, so I sent nothing.")
        _keys(0x11, 0x41)  # Ctrl+A, Delete: no leftover draft gets sent along
        _keys(0x2E)
        typed_in = True
        _type(text)
        time.sleep(0.25)
        typed = _composer_text(box)
        if typed and _fold(text)[:40] not in _fold(typed):
            raise Fail(f"the message box says {typed[:80]!r} instead of the message, so I didn't send it.")
        _esc()  # closes an emoji / @mention suggestion that would otherwise eat the Enter
        time.sleep(0.1)
        c._check_user()
        if user32.GetForegroundWindow() != h:
            raise Fail("Discord lost focus, so I sent nothing.")
        _keys(0x0D)  # send
        want = _fold(text)[-30:]
        for _ in range(16):
            time.sleep(0.25)
            if any(want in _fold(m) for m in _last_messages(h)):
                break
        else:
            return (f"Error: I pressed Enter in {_title(h).removesuffix(' - Discord')!r} but don't see the message "
                    "in the chat (slow mode, a block, or no permission?). Tell the user to check.")
        log.info("sent on discord %s: %r", where, text)
        _restore(prev, h)
        return f"Sent on Discord {where}: \"{text}\""
    except c.TookOver:
        ctx.extra["control_stopped"] = True
        return "STOPPED: the user moved the mouse and took over, nothing was sent."
    except c.Escaped:
        ctx.extra["control_stopped"] = True
        return "STOPPED: the user pressed Esc, nothing was sent."
    except Fail as e:
        _clean_up(h, typed_in)
        return f"Error: {e}"
    finally:
        if own_mode:
            c.end()


def _clean_up(h: int, typed_in: bool) -> None:
    """After a failure: close a quick switcher left open, and don't leave a half-written draft behind."""
    try:
        if h and user32.GetForegroundWindow() == h:
            _esc()
            if typed_in:
                _keys(0x11, 0x41)
                _keys(0x2E)
    except (c.TookOver, c.Escaped):
        pass
