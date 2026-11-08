"""Play music in the Spotify desktop app (no Premium / no login needed).

1. Find the Spotify URI: via the Spotify Web API search if a (free) developer
   client id/secret is configured, otherwise via a web search for open.spotify.com links.
2. Start Spotify if it isn't running.
3. Open the URI in the app and start playback.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from pathlib import Path

import httpx
import psutil

from . import ctx, tool
from .media import get_session, now_playing

log = logging.getLogger("spotify")

LINK_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist|artist)/([A-Za-z0-9]{22})")
_token: tuple[str, float] | None = None


# ── finding things ────────────────────────────────────────────
def _api_token() -> str | None:
    global _token
    sp = (ctx.cfg or {}).get("spotify") or {}
    cid, secret = sp.get("client_id"), sp.get("client_secret")
    if not cid or not secret:
        return None
    if _token and _token[1] > time.time() + 30:
        return _token[0]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {auth}"},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    _token = (j["access_token"], time.time() + j.get("expires_in", 3600))
    return _token[0]


def find(query: str, kind: str = "track") -> tuple[str, str] | None:
    """Returns (spotify_uri, human label) or None."""
    found = find_many(query, kind, 1)
    return found[0] if found else None


def find_many(query: str, kind: str = "track", limit: int = 3) -> list[tuple[str, str]]:
    """Several (uri, label) candidates, best first (the first one may not be playable in your country)."""
    token = _api_token()
    if token:
        r = httpx.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": kind, "limit": limit},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        items = [i for i in r.json().get(f"{kind}s", {}).get("items", []) if i]
        out = []
        for it in items:
            artists = ", ".join(a["name"] for a in it.get("artists", [])) or (it.get("owner") or {}).get("display_name", "")
            out.append((it["uri"], f'{it["name"]}' + (f" by {artists}" if artists else "")))
        if out:
            return out

    from .web import search

    out: list[tuple[str, str]] = []
    phrasings = [f"{query} site:open.spotify.com/{kind}", f"{query} spotify {kind}", f"spotify {kind} {query}"]
    for i, q in enumerate(phrasings + phrasings[:1]):  # one retry at the end (search engines rate-limit)
        if i == len(phrasings):
            time.sleep(1.5)
        try:
            results = search(q, 8)
        except Exception as e:  # noqa: BLE001  (search engines are flaky; try the next phrasing)
            log.debug("search failed for %r: %s", q, e)
            continue
        for res in results:
            m = LINK_RE.search(res.get("href", ""))
            if m and m.group(1) == kind:
                title = res.get("title", "")
                title = re.split(r"\s*\|\s*Spotify|\s*\.\.\.", title)[0].strip()  # search engines glue titles
                # "X - Song, Lyrics, and Music Video by Y", "X - song and lyrics by Y", "X - Album by Y" -> "X by Y"
                title = re.sub(r"\s*[-–|]\s*(?:song|single|album|playlist|ep)\b[^|]*?\bby\s+", " by ", title, flags=re.I)
                title = re.sub(r"\s*[-–|]\s*(?:song|single|album|playlist|ep)\b.*$", "", title, flags=re.I)
                title = re.sub(r"^(?:Listen to|Posłuchaj)\s+", "", title, flags=re.I)
                uri = f"spotify:{kind}:{m.group(2)}"
                if all(u != uri for u, _ in out):
                    out.append((uri, title or query))
                if len(out) >= limit:
                    return out
        if out:
            return out
    return out


# ── the desktop app ───────────────────────────────────────────
def is_running() -> bool:
    return any((p.info.get("name") or "").lower() == "spotify.exe" for p in psutil.process_iter(["name"]))


def launch() -> None:
    sp = (ctx.cfg or {}).get("spotify") or {}
    exe = sp.get("exe_path") or os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe")
    if exe and Path(exe).exists():
        os.startfile(exe)
    else:
        os.startfile("spotify:")  # Microsoft Store version


async def ensure_running() -> bool:
    """Returns True if Spotify had to be started."""
    if is_running():
        return False
    launch()
    for _ in range(60):
        await asyncio.sleep(0.5)
        if is_running() and await get_session() is not None:
            break
    await asyncio.sleep(3)  # let the UI finish loading
    return True


async def _is_now(label: str) -> bool:
    np = await now_playing()
    if not np or np["status"] != "playing":
        return False
    title = np["title"].lower()
    return bool(title) and (title in label.lower() or label.lower().split(" by ")[0] in title)


PLAY_WORDS = ("Play ", "Odtwórz ")      # the big green button: "Play <playlist name>" (EN / PL Spotify)
PAUSE_WORDS = ("Pause ", "Wstrzymaj ")


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", s.lower().replace("’", "'"))


def _spotify_hwnd() -> int | None:
    """Spotify's main window, found with plain Win32 calls (never asks other windows anything:
    a hung window, e.g. a game, used to freeze the UI Automation scan of all windows forever)."""
    import ctypes
    from ctypes import wintypes

    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == "spotify.exe"}
    if not pids:
        return None
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.GetWindowTextLengthW(hwnd) > 0:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                found.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def _spotify_window():
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    hwnd = _spotify_hwnd()
    if not hwnd:
        return None
    try:
        return UIAWrapper(UIAElementInfo(hwnd))
    except Exception:  # noqa: BLE001
        return None


def press_page_play(label: str, timeout: float = 10, cards: bool = True, allow_plain: bool = True) -> str:
    """Playlists/albums/artists open as a page without playing. Press the page's big Play button
    through UI Automation (invoke: no mouse, no focus needed). Returns 'played', 'already' or 'failed'."""
    want = _norm(label.split(" by ")[0])
    end = time.monotonic() + timeout
    fallback_after = time.monotonic() + timeout * 0.6
    while time.monotonic() < end:
        try:
            w = _spotify_window()
            if w is not None:
                big, plain = [], []  # "Play <name>" buttons / unnamed "Play" buttons (album pages)
                for b in w.descendants(control_type="Button"):
                    name = (b.window_text() or "").strip()
                    width = b.rectangle().width()
                    if width < 60:  # the bottom bar's play button and track-row buttons are smaller
                        continue
                    if name.startswith(PLAY_WORDS + PAUSE_WORDS):
                        big.append((name, b))
                    elif name in ("Play", "Pause", "Odtwórz", "Wstrzymaj"):
                        plain.append((width, name + " ", b))
                for name, b in big:
                    page = _norm(name.split(" ", 1)[1])
                    if want and (want in page or page in want):  # the page we just opened
                        if name.startswith(PAUSE_WORDS):
                            return "already"
                        b.invoke()
                        return "played"
                if plain and allow_plain and time.monotonic() > end - timeout + 2.5:  # album/song pages: big unnamed button
                    _w, name, b = max(plain, key=lambda x: x[0])
                    if name.startswith(PAUSE_WORDS):
                        return "already"
                    b.invoke()
                    return "played"
                if cards and big and time.monotonic() > fallback_after:  # names didn't match (translated title?)
                    name, b = max(big, key=lambda nb: nb[1].rectangle().width())
                    if name.startswith(PAUSE_WORDS):
                        return "already"
                    log.info("pressing '%s' (no exact match for '%s')", name, label)
                    b.invoke()
                    return "played"
        except Exception as e:  # noqa: BLE001  (UI still loading)
            log.debug("ui automation: %s", e)
        time.sleep(0.4)
    return "failed"


def _safe_click(window, button) -> bool:
    """A real mouse click on a Spotify element, but only when it can't land anywhere else: Spotify not minimized,
    brought to the front, and the element fully inside Spotify's window and visible. The mouse goes back after.
    (Clicking blindly at an element's position used to click random things when Spotify was hidden or scrolled.)"""
    import ctypes
    from ctypes import wintypes

    from . import control

    try:
        if window.is_minimized():
            return False
        window.set_focus()
        time.sleep(0.25)
        wr, br = window.rectangle(), button.rectangle()
        inside = (br.width() > 4 and br.height() > 4 and br.left >= wr.left and br.right <= wr.right
                  and br.top >= wr.top and br.bottom <= wr.bottom)
        if not inside or not button.is_visible():
            log.info("not clicking '%s': it isn't visible inside Spotify's window", button.window_text())
            return False
        x, y = (br.left + br.right) // 2, (br.top + br.bottom) // 2
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        root = user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(x, y)), 2)  # GA_ROOT
        if not root or int(root) != int(window.handle):  # something else is on top of that spot
            log.info("not clicking '%s': another window covers it", button.window_text())
            return False
        old = control._cursor()
        button.click_input()
        if old:
            ctypes.windll.user32.SetCursorPos(*old)
        control.reset()  # (our own mouse move isn't the user taking over)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("safe click failed: %s", e)
        return False


LIB_RE = re.compile(r"^(.*?)\s+(?:Pinned\s+)?(Playlist|Album|Artist|Podcast)(?:\s+•\s+(.*))?$")


def play_from_library(query: str) -> str | None:
    """Your own playlists/albums/artists in Spotify's sidebar (web search can't find private ones).
    Opens the best match and presses Play. Returns its name, or None if nothing matched."""
    import difflib

    w = _spotify_window()
    if w is None:
        return None
    filler = {"playlist", "playlista", "playlistę", "playliste", "playlisty", "moja", "moją", "moje", "my", "the",
              "na", "z", "ze", "do", "mojej", "spotify", "album", "music", "muzyka", "muzyką"}
    q_words = [_norm(t) for t in query.split() if _norm(t) and _norm(t) not in filler] or [_norm(query)]
    q = "".join(q_words)

    def sound(w: str) -> str:  # spelled differently, sounds the same: sex/seks, vixa/wiksa, hardtek/hardtekk
        for a, b in (("x", "ks"), ("v", "w"), ("ph", "f"), ("ck", "k"), ("qu", "kw"), ("th", "t")):
            w = w.replace(a, b)
        return re.sub(r"(.)\1+", r"\1", w)  # double letters

    def word_match(a: str, b: str) -> bool:
        a, b = sound(a), sound(b)
        return a == b or (len(a) >= 3 and len(b) >= 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75)

    best, score = None, 0.0
    for b in w.descendants(control_type="Button"):
        m = LIB_RE.match(b.window_text() or "")
        if not m:
            continue
        name = m.group(1).strip()
        n = _norm(name)  # (emoji and punctuation drop out here)
        if not n:
            continue
        name_words = [x for x in (_norm(t) for t in name.split()) if x and x not in filler]
        s = 1.0 if n == q else 0.9 if (q in n or n in q) and min(len(q), len(n)) >= 4 else \
            difflib.SequenceMatcher(None, q, n).ratio()
        if name_words and q_words:  # word by word: every word asked for appears in the name
            hits = sum(any(word_match(a, x) for x in name_words) for a in q_words)
            if hits == len(q_words):
                s = max(s, 0.85 + 0.1 * (hits / max(len(name_words), 1)))
        if s > score:
            best, score = (name, b), s
    if not best or score < 0.75:
        return None
    name, b = best
    log.info("library match '%s' for '%s' (%.2f)", name, query, score)
    # open its page, then press *that page's* "Play <name>" (never another page's button)
    for opener in ("invoke", "click"):
        try:
            if opener == "invoke":
                b.invoke()
            elif not _safe_click(w, b):  # a real click only if it lands exactly on that sidebar item
                continue
        except Exception as e:  # noqa: BLE001
            log.debug("library open (%s) failed: %s", opener, e)
            continue
        res = press_page_play(name, timeout=4, cards=False, allow_plain=False)
        if res in ("played", "already"):
            return name
    return None


async def _started(before: dict | None, label: str, track: bool, wait: float) -> bool:
    """Did the new music actually start? (playing, and something changed from before)"""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        await asyncio.sleep(0.4)
        np = await now_playing()
        if not np or np.get("status") != "playing":
            continue
        if track:  # a song: only that song counts (unavailable versions make Spotify skip to the next one)
            if await _is_now(label):
                return True
            continue
        changed = not before or before.get("status") != "playing" or np.get("title") != before.get("title")
        if changed:
            return True
    return False


async def open_and_play(uri: str, label: str) -> bool:
    await ensure_running()
    before = await now_playing()
    track = ":track:" in uri
    if track:
        # a song link only starts by itself when Spotify isn't playing; while it plays, the link just
        # opens the song's page. So pause first, then open it.
        if before and before.get("status") == "playing":
            s = await get_session()
            if s is not None:
                await s.try_pause_async()
                await asyncio.sleep(0.4)
        os.startfile(uri)
        if await _started(before, label, True, 5):
            return True
        np = await now_playing()
        if np and np.get("status") == "playing" and np.get("title") != (before or {}).get("title"):
            return False  # Spotify skipped to another song: this version isn't playable here, try the next one
        await asyncio.sleep(0.5)
    else:
        os.startfile(uri)
    for attempt in range(2):  # press the page's Play button; check it really started; once more if not
        # (for a song never press the "Play <album>" cards in "More by …": that plays a different song)
        try:
            res = await asyncio.wait_for(asyncio.to_thread(press_page_play, label, 8 if attempt == 0 else 5, not track), 20)
        except asyncio.TimeoutError:
            res = "timed out"
        log.info("page play button (try %d): %s", attempt + 1, res)
        if await _started(before, label, track, 4):
            return True
    return False


@tool(
    "Play a song, album, playlist or artist in Spotify (opens Spotify if closed and starts playing).",
    query='what to play, e.g. "Blinding Lights The Weeknd", "lofi beats", "Polski Hardtekk"',
    kind='"track", "album", "playlist" or "artist" (optional, default "track")',
)
async def play_on_spotify(query: str, kind: str = "track") -> str:
    kind = kind if kind in ("track", "album", "playlist", "artist") else "track"
    if kind != "track":  # the user's own library first ("play my playlist X")
        await ensure_running()
        before = await now_playing()
        try:
            name = await asyncio.wait_for(asyncio.to_thread(play_from_library, query), 20)
        except asyncio.TimeoutError:
            log.warning("library lookup took too long; searching Spotify instead")
            name = None
        except Exception as e:  # noqa: BLE001
            log.warning("library lookup failed: %s", e)
            name = None
        if name:
            if await _started(before, name, False, 5):
                return f"Now playing your {kind} '{name}'."
            return f"Your {kind} '{name}' is open in Spotify (it was already playing, or it needs a click on Play)."
    found = await asyncio.to_thread(find_many, query, kind, 3 if kind == "track" else 1)
    if not found:
        return f"Couldn't find '{query}' on Spotify."
    for uri, label in found:  # a version that isn't available in your country gets skipped: try the next
        ctx.hub.emit("music_request", uri=uri, label=label)
        if await open_and_play(uri, label):
            return f"Now playing {label}."
        log.info("'%s' didn't start (%s), trying another version", label, uri)
    return f"Opened {found[0][1]} in Spotify, but it didn't start playing."
