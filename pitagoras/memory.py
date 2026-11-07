"""Long-lived state on disk, read again at every boot.

    personality.txt          who Pitagoras is (edit it any time; used from the next reply)
    memory/conversation.json the recent chat, so it picks up where you left off
    memory/notes.txt         facts it saved with the `remember` tool (editable too)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
from pathlib import Path

from .config import ROOT
from .tools import ctx, tool

log = logging.getLogger("memory")

PERSONALITY = ROOT / "personality.txt"
MEMORY_DIR = ROOT / "memory"
CONVERSATION = MEMORY_DIR / "conversation.json"
NOTES = MEMORY_DIR / "notes.txt"

DEFAULT_PERSONALITY = """\
You are Pitagoras, a small animated character who lives on the user's desktop.
You are witty, warm and a little cheeky, and you love music.
You talk out loud, so write the way people speak: no markdown, no lists, no emoji.
Answer in the language the user speaks.
"""

_cache: dict[Path, tuple[float, str]] = {}


def _read_cached(path: Path) -> str:
    try:
        mt = path.stat().st_mtime
    except OSError:
        return ""
    hit = _cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    text = path.read_text(encoding="utf-8", errors="replace")
    _cache[path] = (mt, text)
    return text


def personality(default: str | None = None) -> str:
    if not PERSONALITY.exists():
        reset_personality(default)
    return _read_cached(PERSONALITY).strip() or DEFAULT_PERSONALITY


def reset_personality(default: str | None = None) -> None:
    PERSONALITY.write_text((default or DEFAULT_PERSONALITY).strip() + "\n", encoding="utf-8")


def notes() -> str:
    return _read_cached(NOTES).strip()


def add_note(fact: str) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    with open(NOTES, "a", encoding="utf-8") as f:
        f.write(f"- {fact.strip()}  ({dt.date.today().isoformat()})\n")


def edit_file(path: Path) -> None:
    """Open a text file in the user's editor (Notepad by default)."""
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    try:
        os.startfile(path, "edit")
    except OSError:
        subprocess.Popen(["notepad.exe", str(path)])


# ── conversation ─────────────────────────────────────────────
def _text_only(m: dict) -> dict:
    c = m["content"]
    if isinstance(c, list):  # drop screenshots, keep the words (no marker: models copy those)
        c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
    return {"role": m["role"], "content": c}


def save_conversation(history: list[dict], keep: int = 200) -> None:
    MEMORY_DIR.mkdir(exist_ok=True)
    data = {"saved": dt.datetime.now().isoformat(timespec="minutes"),
            "messages": [_text_only(m) for m in history[-keep:]]}
    tmp = CONVERSATION.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CONVERSATION)


def load_conversation() -> tuple[list[dict], str | None]:
    try:
        data = json.loads(CONVERSATION.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    msgs = [m for m in data.get("messages", []) if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    return msgs, data.get("saved")


def clear_conversation() -> None:
    CONVERSATION.unlink(missing_ok=True)


def clear_all() -> None:
    clear_conversation()
    NOTES.unlink(missing_ok=True)


@tool("Save a lasting fact to your long-term memory (the user's name, likes, plans, things they ask you to remember). "
      "It is shown to you in every future conversation.", fact="the fact, as a short sentence")
def remember(fact: str) -> str:
    if not (ctx.cfg.get("memory") or {}).get("enabled", True):
        return "Memory is turned off in the settings."
    add_note(fact)
    return "saved to memory"
