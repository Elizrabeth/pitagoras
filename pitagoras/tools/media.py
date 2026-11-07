"""Windows media session control (works with Spotify, browsers, any media app)
plus volume keys. Also used by the now-playing watcher."""
from __future__ import annotations

import ctypes
import logging

from . import tool

log = logging.getLogger("media")

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
except Exception:  # noqa: BLE001
    MediaManager = None

# GlobalSystemMediaTransportControlsSessionPlaybackStatus
_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}

VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def press_key(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


async def get_session(prefer: str = "spotify"):
    if MediaManager is None:
        return None
    mgr = await MediaManager.request_async()
    for s in mgr.get_sessions():
        if prefer.lower() in (s.source_app_user_model_id or "").lower():
            return s
    return mgr.get_current_session()


async def now_playing() -> dict | None:
    s = await get_session()
    if s is None:
        return None
    props = await s.try_get_media_properties_async()
    info = s.get_playback_info()
    status = _STATUS.get(int(info.playback_status), "unknown") if info else "unknown"
    return {
        "title": props.title or "",
        "artist": props.artist or "",
        "album": props.album_title or "",
        "status": status,
        "app": s.source_app_user_model_id or "",
    }


@tool(
    "Control music/media playback.",
    action='"play", "pause", "next", "previous" or "toggle"',
)
async def media_control(action: str) -> str:
    action = action.lower().strip()
    s = await get_session()
    if s is not None:
        fn = {
            "play": s.try_play_async,
            "pause": s.try_pause_async,
            "next": s.try_skip_next_async,
            "previous": s.try_skip_previous_async,
            "toggle": s.try_toggle_play_pause_async,
        }.get(action)
        if fn is None:
            return f"Unknown action {action}"
        await fn()
        return f"{action} ok"
    key = {"next": "next", "previous": "previous"}.get(action, "play_pause")
    press_key(VK[key])
    return f"{action} ok (media key)"


@tool("Say what song is playing right now.")
async def what_is_playing() -> str:
    np = await now_playing()
    if not np or not np["title"]:
        return "Nothing is playing."
    return f'{np["status"]}: "{np["title"]}" by {np["artist"]} ({np["app"]})'


@tool(
    "Change the system volume.",
    direction='"up", "down" or "mute"',
    steps="how much, 1-10 (optional, default 3)",
)
def volume(direction: str, steps: int = 3) -> str:
    direction = direction.lower()
    if direction == "mute":
        press_key(VK["mute"])
        return "toggled mute"
    vk = VK["volume_up"] if direction == "up" else VK["volume_down"]
    press_key(vk, max(1, min(25, int(steps))) * 2)  # one key press = 2%
    return f"volume {direction}"
