from __future__ import annotations

import datetime as dt

from . import tool


@tool("Get the current date and time.")
def get_time() -> str:
    return dt.datetime.now().strftime("%A %d %B %Y, %H:%M")


@tool("Open/launch an installed app or game by name (Steam games too: it starts Steam if needed). "
      "If it isn't installed, its website is opened instead. "
      "'Open steam and play Deadlock' = one call with name \"Deadlock\". Speech recognition can garble names "
      "('z tima' / 'steema' = Steam): use the closest real name to what was said, never a different game.",
      name="just the app or game name, e.g. Deadlock, Steam, Discord, Photoshop, notepad")
def open_app(name: str) -> str:
    from .. import apps
    from .web import open_site

    name = apps.clean_name(name)
    hit = apps.open_by_name(name)
    if hit:
        kind = "Steam game" if hit["kind"] == "game" else "app"
        return f"Launched {hit['name']} ({kind})."
    url = open_site(name)
    return f"'{name}' is not installed, so I opened its website instead: {url}"


@tool("List installed games or apps (to answer 'what games do I have?' or find the exact name).",
      kind="games or apps", filter="part of the name (optional)")
def list_installed(kind: str = "games", filter: str = "") -> str:
    from .. import apps

    k = "app" if kind.lower().startswith("app") else "game"
    items = apps.find(filter, k, limit=40) if filter.strip() else [i for i in apps.index() if i["kind"] == k]
    names = sorted({i["name"] for i in items}, key=str.lower)
    if not names:
        return f"No {kind} found" + (f" matching '{filter}'." if filter else ".")
    return f"{len(names)} {kind}: " + ", ".join(names[:80])
