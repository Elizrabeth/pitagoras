"""Tools that let the brain drive the visual character. They only emit events;
your character app decides what they look like."""
from __future__ import annotations

from . import ctx, tool

EMOTIONS = ["happy", "sad", "excited", "angry", "surprised", "thinking", "love", "sleepy", "dance", "neutral"]
POSITIONS = ["left", "right", "center", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"]


@tool(f"Show an emotion/animation on your character. One of: {', '.join(EMOTIONS)}.", instant=True, emotion="the emotion")
def emote(emotion: str) -> str:
    ctx.hub.emit("emotion", emotion=emotion.lower().strip())
    return f"showing {emotion}"


@tool(
    f"Move YOUR OWN CHARACTER (the yellow ball) around the screen. One of: {', '.join(POSITIONS)}, 'follow_mouse', "
    "or 'teleport'. This does NOT move the mouse and does NOT click anything (for that use click_on / click).",
    instant=True,
    position="where to go",
)
def move_to(position: str) -> str:
    ctx.hub.emit("move", position=position.lower().strip())
    return f"moving to {position}"
