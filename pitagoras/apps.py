"""Knows what's installed, so "open steam", "launch deadlock" or "open photoshop" just work.

Sources: the Start menu (Get-StartApps: desktop apps, Store apps and Steam
shortcuts) and Steam's own library manifests (games without a shortcut).
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("apps")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Built into Windows, or common names that don't match the Start-menu name.
BUILTIN = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "task manager": "taskmgr.exe", "file explorer": "explorer.exe", "explorer": "explorer.exe",
    "files": "explorer.exe", "settings": "ms-settings:", "control panel": "control.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "terminal": "wt.exe", "powershell": "powershell.exe",
    "snipping tool": "ms-screenclip:", "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
    # Polish names (an English Windows' Start menu only knows the English ones); word endings are handled
    # by pl_forms(), so "notatnika", "kalkulatorem", "przeglądarkę" find these too
    "notatnik": "notepad.exe", "kalkulator": "calc.exe", "menedżer zadań": "taskmgr.exe",
    "menadżer zadań": "taskmgr.exe", "eksplorator plików": "explorer.exe", "eksplorator": "explorer.exe",
    "pliki": "explorer.exe", "folder": "explorer.exe", "ustawienia": "ms-settings:", "panel sterowania": "control.exe",
    "wiersz polecenia": "cmd.exe", "konsola": "cmd.exe", "terminal windows": "wt.exe", "wycinanie": "ms-screenclip:",
    "narzędzie wycinanie": "ms-screenclip:", "zrzut ekranu": "ms-screenclip:", "sklep": "ms-windows-store:",
    "sklep microsoft": "ms-windows-store:", "przeglądarka": "https://www.google.com", "przeglądarka internetowa": "https://www.google.com",
    "kalendarz": "outlookcal:", "poczta": "outlookmail:", "zdjęcia": "ms-photos:", "galeria": "ms-photos:",
    "aparat": "microsoft.windows.camera:", "kamera": "microsoft.windows.camera:", "zegar": "ms-clock:",
    "alarmy": "ms-clock:", "minutnik": "ms-clock:", "mapy": "bingmaps:", "pogoda": "bingweather:",
    "nagrywanie dźwięku": "ms-callrecording:", "dyktafon": "ms-callrecording:", "edytor tekstu": "notepad.exe",
    "ustawienia dźwięku": "ms-settings:sound", "ustawienia wyświetlania": "ms-settings:display",
    "bluetooth": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi", "aktualizacje": "ms-settings:windowsupdate",
    "kosz": "shell:RecycleBinFolder", "pobrane": "shell:Downloads", "dokumenty": "shell:Personal",
    "pulpit": "shell:Desktop", "obrazy": "shell:My Pictures", "muzyka": "shell:My Music", "wideo": "shell:My Video",
}

# Polish case endings, longest first: "notatnik-a", "kalkulator-em", "przeglądark-ę" -> "przeglądark-a"
PL_ENDINGS = [("ami", ""), ("ach", ""), ("owi", ""), ("iem", ""), ("em", ""), ("om", ""), ("ów", ""), ("ie", ""),
              ("ę", "a"), ("ą", "a"), ("y", "a"), ("i", "a"), ("ki", "ka"), ("a", ""), ("u", ""), ("e", ""),
              ("'a", ""), ("'em", ""), ("’a", "")]


def pl_forms(name: str) -> list[str]:
    """The name as said plus its likely base forms: 'odpal notatnika' -> notatnika, notatnik, …
    Each word is tried with its ending removed/replaced (only words of 5+ letters, so short names stay)."""
    words = name.lower().split()
    forms = [" ".join(words)]
    for i, w in enumerate(words):
        for end, base in PL_ENDINGS:
            if len(w) >= 5 and w.endswith(end):
                cand = words[:i] + [w[: len(w) - len(end)] + base] + words[i + 1:]
                forms.append(" ".join(cand))
    return list(dict.fromkeys(f for f in forms if f.strip()))
ALIASES = {"vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
           "chrome": "google chrome", "edge": "microsoft edge", "firefox": "mozilla firefox",
           "word": "microsoft word", "excel": "microsoft excel", "powerpoint": "microsoft powerpoint",
           "ps": "photoshop", "obs": "obs studio", "gmod": "garry's mod", "isaac": "binding of isaac",
           "riot games": "riot client", "riot": "riot client", "lol": "league of legends",
           "liga": "league of legends", "ligę": "league of legends", "valorant": "valorant"}
GENERIC_SECOND = {"games", "game", "client", "launcher", "app", "aplikacja", "studio", "store", "connect"}
SKIP = re.compile(r"uninstall|readme|release notes|help|manual|documentation|support center|website", re.I)
STEAM_SKIP = re.compile(r"redistributable|steamworks|proton|steam linux runtime", re.I)

_index: list[dict] = []
_built = 0.0
_lock = threading.Lock()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _start_apps() -> list[dict]:
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
          "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
    data = json.loads(r.stdout or "[]")
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        name, appid = (d.get("Name") or "").strip(), d.get("AppID") or ""
        if not name or not appid or SKIP.search(name) or appid.startswith(("http://", "https://")):
            continue
        out.append({"name": name, "appid": appid, "kind": "game" if appid.startswith("steam://") else "app"})
    return out


def steam_path() -> Path | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None


def _steam_games() -> list[dict]:
    root = steam_path()
    if not root:
        return []
    libs = {root}
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            libs.add(Path(m.group(1).replace("\\\\", "\\")))
    out = []
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            txt = acf.read_text(encoding="utf-8", errors="replace")
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            if appid and name and not STEAM_SKIP.search(name.group(1)):
                out.append({"name": name.group(1), "appid": f"steam://rungameid/{appid.group(1)}", "kind": "game"})
    return out


def build_index() -> list[dict]:
    global _index, _built
    with _lock:
        items: dict[str, dict] = {}
        for src in (_steam_games, _start_apps):  # Start-menu entries win on duplicates
            try:
                for it in src():
                    items[norm(it["name"]) + it["kind"]] = it
            except Exception as e:  # noqa: BLE001
                log.warning("app scan (%s) failed: %s", src.__name__, e)
        _index = list(items.values())
        _built = time.monotonic()
        log.info("found %d apps and games", len(_index))
        return _index


def index() -> list[dict]:
    return _index if _index else build_index()


def _score(q: str, words: list[str], name: str) -> float:
    n = norm(name)
    if not n:
        return 0
    if n == q:
        return 100
    if n.startswith(q):
        return 85 - min(15, len(n) - len(q)) * 0.5
    if q in n:
        return 72 - min(12, len(n) - len(q)) * 0.3
    if words and all(w in n for w in words):
        return 65
    r = difflib.SequenceMatcher(None, q, n).ratio()
    return r * 70 if r > 0.78 else 0


def find(name: str, kind: str | None = None, limit: int = 5) -> list[dict]:
    raw = name.lower().strip()
    raw = ALIASES.get(raw, raw)
    q = norm(raw)
    words = [norm(w) for w in raw.split() if len(norm(w)) > 1]
    if not q:
        return []
    scored = []
    for it in index():
        s = _score(q, words, it["name"])
        if s and (kind is None or it["kind"] == kind):
            scored.append((s, it))
    scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [it for s, it in scored[:limit] if s >= 55]


def launch(it: dict) -> None:
    appid = it["appid"]
    if re.match(r"^[a-z][\w+.-]*:", appid) and not re.match(r"^[a-zA-Z]:\\", appid):  # URI (steam://…)
        os.startfile(appid)
    elif os.path.isfile(appid):
        os.startfile(appid)
    else:  # Store / packaged app
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{appid}"])


LAUNCHER_RE = re.compile(r"^(?:open |launch |start )?(?:steam|epic(?: games)?|the steam app)\s*(?:,|and|then|&)\s*"
                         r"(?:then )?(?:play|launch|start|open|run|turn on|boot up|fire up)?\s*", re.I)
VERB_RE = re.compile(r"^(?:play|launch|start|open|run|turn on|boot up|fire up)\s+(?:the )?(?:game )?", re.I)


def clean_name(name: str) -> str:
    """'steam and play Deadlock' -> 'Deadlock' (Steam games start Steam by themselves)."""
    n = name.strip().strip("'\".")
    rest = LAUNCHER_RE.sub("", n)
    if rest and rest != n:
        n = rest
    return VERB_RE.sub("", n).strip() or name.strip()


def open_by_name(name: str) -> dict | None:
    """Launch the best match. Returns the entry, or None if nothing matched."""
    name = clean_name(name)
    forms = pl_forms(name)  # "notatnika" -> "notatnik", "przeglądarkę" -> "przeglądarka"
    for key in forms:
        if key in BUILTIN:
            target = BUILTIN[key]
            if re.match(r"^[\w.+-]+:", target) and not target.lower().endswith(".exe"):  # URI / shell: / ms-…:
                os.startfile(target)
            else:
                subprocess.Popen(target, shell=True)
            return {"name": key, "kind": "app"}
    hits = find(name)
    for f in forms[1:]:  # the Start menu name may only match the base form ("discorda" -> "discord")
        if hits:
            break
        hits = find(f)
    if not hits and time.monotonic() - _built > 60:  # maybe installed since we last looked
        build_index()
        hits = find(name)
    parts = name.lower().split()
    if not hits and len(parts) == 2 and parts[1] in GENERIC_SECOND:
        # "Riot Games" -> "Riot Client", "Epic Games" -> "Epic Games Launcher" (brand + a generic word only:
        # "Team Fortress 2" must NOT open "Teamfight Tactics")
        first = norm(parts[0])
        if len(first) >= 4:
            hits = sorted((i for i in index() if norm(i["name"]).startswith(first)), key=lambda i: len(i["name"]))
    if not hits:
        return None
    launch(hits[0])
    return hits[0]
