"""Tool registry. Add a tool anywhere with:

    @tool("What it does", query="what to search for")
    def my_tool(query: str) -> str: ...

Sync tools run in a worker thread, async tools run on the event loop.
Tools can use `ctx.hub` (events to the character) and `ctx.cfg` (config).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("tools")


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]
    fn: Callable[..., Any]
    instant: bool = False  # fire-and-forget: runs mid-reply, result not sent back to the model


@dataclass
class Context:
    hub: Any = None
    cfg: Any = None
    extra: dict = field(default_factory=dict)


TOOLS: dict[str, Tool] = {}
ctx = Context()


def tool(description: str, instant: bool = False, **params: str):
    """Register a function as a tool. Keyword args describe its parameters
    (append '(optional)' to the description of optional ones).
    instant=True: the tool runs while the model keeps talking (emotes, moves)."""

    def deco(fn):
        TOOLS[fn.__name__] = Tool(fn.__name__, description, params, fn, instant)
        return fn

    return deco


def describe_tools() -> str:
    lines = []
    for t in TOOLS.values():
        args = ", ".join(f'"{k}": {v}' for k, v in t.params.items())
        lines.append(f"- {t.name}({{{args}}}): {t.description}")
    return "\n".join(lines)


async def run_tool(name: str, args: dict) -> str | dict:
    """Returns text, or {"text": ..., "image": <base64 jpeg>} for tools that show the model a picture."""
    t = TOOLS.get(name)
    if not t:
        return f"Error: unknown tool '{name}'. Available: {', '.join(TOOLS)}"
    sig = inspect.signature(t.fn)
    args = {k: v for k, v in (args or {}).items() if k in sig.parameters}
    try:
        if inspect.iscoroutinefunction(t.fn):
            result = await t.fn(**args)
        else:
            result = await asyncio.to_thread(t.fn, **args)
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return f"Error: {name} failed: {e}"
    if isinstance(result, dict):
        return result
    return str(result) if result is not None else "done"


def load_all() -> None:
    """Import tool modules so their @tool decorators register."""
    from . import character, control, media, spotify, system, timers, web  # noqa: F401
    from .. import eyes, memory, watch  # noqa: F401
