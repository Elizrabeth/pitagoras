"""Watch along: Pitagoras watches a video/stream/game with you and reacts now and then.

Each comment is based on a screenshot (if the brain can see), the title of the
active window, what's playing, and what was SAID in the video since the last
comment (system audio -> Whisper). How often it speaks is the "chatter" setting.
It replies PASS to stay quiet, so it doesn't talk just to talk.
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import logging
import re
import threading
import time

import numpy as np

from .tools import ctx, tool

log = logging.getLogger("watch")

# chatter -> (seconds between comments, extra instruction)
CHATTER = {
    "quiet": (100, "Only speak if something genuinely notable, funny or surprising happens; otherwise PASS."),
    "normal": (45, "Comment when there's something worth reacting to."),
    "chatty": (18, "You're an excited, talkative watching buddy: react often (but never more than one line)."),
}

WATCH_PROMPT = (
    "[Watch-along: you are watching this together with the user. They didn't say anything just now.]\n"
    "{context}\n"
    "First write a private note inside <think></think>: ONE short sentence about what is happening right now, "
    "concretely (who/what is on screen, the moment or play, the score if you can see it). It is never spoken; "
    "it's your memory, so you can explain your comment later if the user asks. "
    "Then react like a friend watching with them: ONE short spoken line (a reaction, joke, opinion or question). "
    "Don't describe the screen literally and don't repeat your earlier comments. "
    "{chatter} If there's nothing worth saying, write the note and then exactly: PASS\n"
    "Example of a note: <think>Red team's striker just missed an open goal from close range, still 1:0.</think>"
)


def active_window_title() -> str:
    try:
        user32 = ctypes.windll.user32
        h = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, buf, 512)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


class AudioTap:
    """Rolling buffer of the PC's sound (fed by MusicSense's loopback stream)."""

    def __init__(self, seconds: int = 90):
        self.seconds = seconds
        self.chunks: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        self.sr = 48000
        self.enabled = False
        self.lock = threading.Lock()

    def push(self, x: np.ndarray, sr: int) -> None:
        now = time.monotonic()
        with self.lock:
            self.sr = sr
            self.chunks.append((now, x.astype(np.float32, copy=False)))
            while self.chunks and now - self.chunks[0][0] > self.seconds:
                self.chunks.popleft()

    def since(self, t: float) -> np.ndarray:
        """Audio captured after time t, resampled to 16 kHz mono."""
        with self.lock:
            parts = [x for ts, x in self.chunks if ts > t]
            sr = self.sr
        if not parts:
            return np.zeros(0, np.float32)
        x = np.concatenate(parts)
        if sr == 16000:
            return x
        n = int(len(x) * 16000 / sr)
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


TAP = AudioTap()


def transcribe(audio: np.ndarray, voice_cfg) -> str:
    """What was said in the PC audio (empty if nothing / Whisper unavailable)."""
    if len(audio) < 16000 * 2 or float(np.sqrt(np.mean(audio * audio))) < 0.003:
        return ""
    from .voice import stt

    model = next(iter(stt._MODELS.values()), None) or stt.load_whisper(voice_cfg)
    segs, _ = model.transcribe(audio, beam_size=1, vad_filter=True, condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6).strip()
    if re.sub(r"[^\w\s]", "", text.lower()).strip() in stt.HALLUCINATIONS:
        return ""
    return text[-1500:]


class Watcher:
    def __init__(self, app):
        self.app = app
        self.active = False
        self.last = 0.0          # when the last comment turn started
        self.now_playing: dict = {}

    def chatter(self) -> str:
        c = (self.app.cfg.get("watch") or {}).get("chatter", "normal")
        return c if c in CHATTER else "normal"

    def set(self, on: bool) -> None:
        self.active = bool(on)
        TAP.enabled = self.active
        if not on:
            with TAP.lock:
                TAP.chunks.clear()
        self.last = time.monotonic() - CHATTER[self.chatter()][0] * 0.6  # first comment comes sooner
        log.info("watch along %s", "on" if on else "off")

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            if not self.active:
                continue
            try:
                await self._maybe_comment()
            except Exception:  # noqa: BLE001
                log.exception("watch comment failed")

    async def _maybe_comment(self) -> None:
        app, agent = self.app, self.app.agent
        now = time.monotonic()
        interval, rule = CHATTER[self.chatter()]
        speaking = app.speaker and app.speaker.playing
        if agent is None or agent.brain is None or agent.busy or speaking:
            return
        if now - agent.last_activity < 8 or now - self.last < interval:
            return
        title = active_window_title()
        if title == "Pitagoras":  # the character itself has focus
            title = ""
        np_ = self.now_playing
        if np_.get("status") == "paused" and np_.get("title") and np_["title"].lower() in title.lower():
            return  # the video they're watching is paused: wait
        since, self.last = self.last, now

        from .eyes import can_see, capture

        img = None
        if can_see()[0]:
            img = await asyncio.to_thread(capture, ctx.extra.get("char_pos"))
        heard = ""
        if (app.cfg.get("watch") or {}).get("hear_audio", True):
            audio = TAP.since(max(since, now - 60))
            try:
                heard = await asyncio.to_thread(transcribe, audio, app.cfg.voice)
            except Exception as e:  # noqa: BLE001
                log.warning("could not transcribe the video audio: %s", e)
        lines = []
        if title:
            lines.append(f'Active window: "{title}"')
        if np_.get("title") and np_.get("status") == "playing":
            lines.append(f'Playing: "{np_["title"]}"' + (f' by {np_["artist"]}' if np_.get("artist") else ""))
        if heard:
            lines.append(f'What was said in the last {int(now - max(since, now - 60))}s: "{heard}"')
        if img:
            lines.append("A screenshot of the screen is attached.")
        if not (img or heard):
            log.info("(watching, but can't see or hear anything to react to)")
            return
        log.info("watch-along look: window=%r, heard %d chars, screenshot=%s", title[:60], len(heard), bool(img))
        prompt = WATCH_PROMPT.format(context="\n".join(lines), chatter=rule)
        await agent.submit(prompt, image=img, auto=True)


@tool("Start or stop watching along with the user (you react now and then to the video, stream or game "
      "on their screen). Use when they say e.g. 'watch this with me' or 'stop commenting'.",
      on="true to start, false to stop")
async def watch_along(on: bool = True) -> str:
    app = ctx.extra.get("app")
    if not app:
        return "watch along is not available"
    on = on if isinstance(on, bool) else str(on).lower() not in ("false", "0", "no", "off", "stop")
    await app.set_watch(on)
    return "watching along now (you'll comment now and then)" if on else "stopped watching along"
