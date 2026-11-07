"""Timers: "xayah flashneła, trackuj" -> it looks up Flash's cooldown and counts it down; when it's up it says
"Flash jest gotowy na Xayah". Works for anything ("przypomnij mi za 10 minut o pizzy").

The countdown starts when YOU asked (the moment your request came in), so time spent searching the web
for the cooldown is already subtracted. Running timers are shown under the character.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time

from . import ctx, tool

log = logging.getLogger("timers")

_ids = itertools.count(1)
TIMERS: dict[int, dict] = {}  # id -> {label, ends (monotonic), total, message, task}


def _emit() -> None:
    now = time.monotonic()
    ctx.hub.emit("timers", timers=[{"id": i, "label": t["label"], "left": round(t["ends"] - now, 1),
                                     "total": t["total"]} for i, t in sorted(TIMERS.items(), key=lambda x: x[1]["ends"])])


def _fmt(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"


async def _ring(tid: int) -> None:
    t = TIMERS.get(tid)
    if not t:
        return
    try:
        await asyncio.sleep(max(0.0, t["ends"] - time.monotonic()))
    except asyncio.CancelledError:
        return
    TIMERS.pop(tid, None)
    _emit()
    msg = t["message"] or f"{t['label']}: time's up!"
    log.info("timer done: %s", t["label"])
    agent = ctx.extra.get("agent")
    if agent is not None:
        agent.announce(msg)
    else:
        ctx.hub.emit("toast", text=msg)


def _find(label: str) -> list[int]:
    from .control import _fold

    want = _fold(label)
    return [i for i, t in TIMERS.items() if want and (want in _fold(t["label"]) or _fold(t["label"]) in want)]


@tool("Start a countdown timer, e.g. to track a cooldown in a game (look the cooldown up first with web_search "
      "if you're not sure) or a reminder. When it ends you say 'message' out loud. Time already spent on this "
      "request (e.g. searching) is subtracted automatically, so give the FULL duration.",
      seconds="full duration in seconds (e.g. 300 for 5 minutes)",
      label="short name, e.g. 'Flash Xayah'",
      message="what to say when it's done, in the user's language, e.g. 'Flash jest gotowy na Xayah!'")
async def start_timer(seconds: float, label: str = "timer", message: str = "") -> str:
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "Error: seconds must be a number."
    if not 1 <= total <= 24 * 3600:
        return "Error: the timer must be between 1 second and 24 hours."
    started = ctx.extra.get("request_started") or time.monotonic()
    already = max(0.0, time.monotonic() - started)
    for old in _find(label):  # the same thing again (e.g. she flashed again): restart it
        t = TIMERS.pop(old)
        t["task"].cancel()
    tid = next(_ids)
    TIMERS[tid] = {"label": label, "ends": started + total, "total": total, "message": message}
    TIMERS[tid]["task"] = asyncio.get_running_loop().create_task(_ring(tid))
    _emit()
    log.info("timer '%s' %ss (%.1fs already passed)", label, total, already)
    return f"Timer '{label}' running: {_fmt(total - already)} left ({already:.0f}s were already used up)."


@tool("Cancel a running timer.", label="its name (or part of it), or 'all'")
async def cancel_timer(label: str) -> str:
    ids = list(TIMERS) if str(label).lower() in ("all", "wszystkie") else _find(label)
    if not ids:
        return f"No timer called '{label}'."
    for i in ids:
        TIMERS.pop(i)["task"].cancel()
    _emit()
    return f"Cancelled {len(ids)} timer(s)."


@tool("List running timers and how much time is left.")
async def list_timers() -> str:
    if not TIMERS:
        return "No timers running."
    now = time.monotonic()
    return "; ".join(f"{t['label']}: {_fmt(t['ends'] - now)} left" for t in sorted(TIMERS.values(), key=lambda t: t["ends"]))
