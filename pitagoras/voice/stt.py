"""Always-on microphone listener: voice activity detection + faster-whisper."""
from __future__ import annotations

import collections
import logging
import os
import queue
import re
import site
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from ..config import Cfg

log = logging.getLogger("stt")

SR = 16000
FRAME = 480  # 30 ms

# Things Whisper likes to "hear" in silence/noise.
HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye", "so",
    "subtitles by the amara.org community", "dziękuję", "dziękuję za uwagę", "napisy stworzone przez społeczność amara.org",
}


def _add_cuda_dlls() -> None:
    """faster-whisper needs cuBLAS/cuDNN; the pip nvidia-* packages ship them."""
    if sys.platform != "win32":
        return
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        base = Path(sp) / "nvidia"
        if base.is_dir():
            for bin_dir in base.glob("*/bin"):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


_MODELS: dict[str, object] = {}


def load_whisper(cfg: Cfg, size: str | None = None):
    size = size or cfg.get("stt_model", "small")
    if size not in _MODELS:
        _MODELS.clear()  # keep only one in (V)RAM
        _MODELS[size] = _load_whisper(cfg, size)
    return _MODELS[size]


def _load_whisper(cfg: Cfg, size: str):
    from faster_whisper import WhisperModel

    if cfg.get("stt_device", "cuda") == "cuda":
        _add_cuda_dlls()
        try:
            m = WhisperModel(size, device="cuda", compute_type="float16")
            m.transcribe(np.zeros(SR, np.float32))  # fail fast if CUDA kernels don't run
            log.info("stt: whisper %s on GPU", size)
            return m
        except Exception as e:  # noqa: BLE001
            log.warning("whisper on GPU failed (%s); using CPU", e)
    log.info("stt: whisper %s on CPU", size)
    return WhisperModel(size, device="cpu", compute_type="int8")


class Listener(threading.Thread):
    def __init__(self, cfg: Cfg, on_text: Callable[[str], None], emit: Callable[..., None], size: str | None = None,
                 wake_check: Callable[[str], str | None] | None = None, on_wake: Callable[[], None] | None = None):
        super().__init__(daemon=True, name="stt")
        self.cfg = cfg
        self.on_text = on_text
        self.emit = emit
        # wake_check(partial text) -> None (not for me) / "phrase" (just "hej pitagoras" so far) / "prompt"
        self.wake_check = wake_check
        self.live_wake: str | None = None
        self.on_wake = on_wake or (lambda: None)
        self.model = load_whisper(cfg, size)
        self._stop = threading.Event()
        self.error: str | None = None
        self.ready = threading.Event()
        self.language = cfg.get("language") or None
        self.languages = [lang.lower() for lang in (cfg.get("languages") or [])]
        if cfg.get("ears_language") in ("pl", "en"):  # Settings > I speak
            self.language = cfg.get("ears_language")
        # teach Whisper the name, or it hears "witaj go roz" instead of "hey Pitagoras"
        phrase = str(cfg.get("wake_phrase") or "hej pitagoras")
        self.hotwords = (cfg.get("hotwords") or "Hej Pitagoras, Pitagoras") if "pitagoras" in phrase.lower() \
            else f"{phrase.title()}, {phrase.split()[-1].title()}"  # a custom wake phrase
        self.vocab = cfg.get("vocab") or "Steam, Spotify, Discord, Riot Client, League of Legends, YouTube, Chrome"
        self.silence_ms = int(cfg.get("silence_ms", 1200))
        self.min_level = float(cfg.get("min_level", 0.012))
        self.device = cfg.get("input_device")
        self.paused = threading.Event()
        self.audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def pause(self) -> None:
        self.paused.set()

    def resume(self, delay: float = 0.25) -> None:
        def _go():
            time.sleep(delay)  # let the speaker's echo die out
            with self.audio_q.mutex:
                self.audio_q.queue.clear()
            self.paused.clear()

        threading.Thread(target=_go, daemon=True).start()

    def _callback(self, indata, frames, t, status):  # audio thread
        if not self.paused.is_set():
            self.audio_q.put(indata[:, 0].copy())

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=FRAME,
                                    device=self.device, callback=self._callback)
        except Exception as e:  # noqa: BLE001
            self.error = f"Could not open the microphone: {e}"
            log.error(self.error)
            self.ready.set()
            return
        with stream:
            log.info("listening on mic %s", self.device if self.device is not None else "(default)")
            self.ready.set()
            self._loop()

    def _loop(self) -> None:
        pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=10)  # 300 ms
        floor = self.min_level
        speech: list[np.ndarray] = []
        loud_frames = silent_ms = 0
        recording = False
        last_level_emit = 0.0
        wake: str | None = None     # what the live check found in this sentence so far
        checked_at = 0              # frames recorded at the last live check
        lang: str | None = None

        while not self._stop.is_set():
            try:
                frame = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                if self.paused.is_set() and recording:
                    recording, speech, loud_frames = False, [], 0
                    self.emit("user_speaking", active=False)
                continue

            rms = float(np.sqrt(np.mean(frame * frame)))
            threshold = max(self.min_level, floor * 3.0)
            loud = rms > threshold

            now = time.monotonic()
            if now - last_level_emit > 0.066:
                self.emit("mic", level=round(min(1.0, rms * 8), 3))
                last_level_emit = now

            if not recording:
                floor = 0.97 * floor + 0.03 * min(rms, threshold)  # adapt to room noise
                pre_roll.append(frame)
                loud_frames = loud_frames + 1 if loud else 0
                if loud_frames >= 3:  # ~90 ms of sound
                    recording, silent_ms = True, 0
                    speech = list(pre_roll)
                    wake, checked_at, lang = None, 0, None
                    self.emit("user_speaking", active=True)
                continue

            speech.append(frame)
            silent_ms = 0 if loud else silent_ms + 30

            # live check while you're still talking: did the sentence start with "hej pitagoras"?
            n = len(speech)
            if (self.wake_check and wake != "prompt" and n >= 25 and n - checked_at >= 18 and n <= 150):
                checked_at = n
                if lang is None:
                    lang = self._pick_language(np.concatenate(speech)) or ""
                partial = self._quick_text(np.concatenate(speech), lang or None)
                found = self.wake_check(partial) if partial else None
                if found and wake is None:
                    log.info("wake phrase heard (live): %s", partial)
                    self.on_wake()
                wake = found or wake

            # 4.5 s in and still no "hej pitagoras": it's background talk, a video or a call. Drop it now
            # instead of transcribing all of it (that took up to ~10 s and kept the ears busy).
            if self.wake_check and wake is None and n > 150:
                recording, loud_frames, speech = False, 0, []
                pre_roll.clear()
                self.emit("user_speaking", active=False)
                log.debug("(not for me, dropped)")
                continue

            # how long a pause ends the sentence: wait longer right after "hej pitagoras",
            # answer sooner once the request is there
            need = self.silence_ms
            if wake == "phrase":
                need = max(self.silence_ms, 2500)
            elif wake == "prompt":
                need = min(self.silence_ms, 900)
            too_long = len(speech) * 30 > 30_000
            if silent_ms >= need or too_long:
                recording, loud_frames = False, 0
                self.emit("user_speaking", active=False)
                audio = np.concatenate(speech)
                speech = []
                pre_roll.clear()
                if len(audio) / SR >= 0.5:
                    self.live_wake = wake  # the final transcript can mangle the name; the live check heard it
                    self._transcribe(audio)
                wake = None

    def _pick_language(self, audio: np.ndarray) -> str | None:
        """Short phrases make Whisper guess wild languages (es, pt…); choose among the ones the user speaks."""
        if self.language or not self.languages:
            return self.language
        try:
            _, _, probs = self.model.detect_language(audio)
        except Exception:  # noqa: BLE001
            return None
        allowed = [(lang, p) for lang, p in probs if lang in self.languages]
        return max(allowed, key=lambda x: x[1])[0] if allowed else self.languages[0]

    def _quick_text(self, audio: np.ndarray, language: str | None) -> str:
        """Fast rough transcript of the sentence so far (for spotting the wake phrase)."""
        try:
            segs, _ = self.model.transcribe(audio, language=language, beam_size=1, vad_filter=False,
                                            condition_on_previous_text=False, without_timestamps=True,
                                            hotwords=self.hotwords)
            return " ".join(s.text.strip() for s in segs).strip()
        except Exception:  # noqa: BLE001
            return ""

    def _transcribe(self, audio: np.ndarray) -> None:
        t0 = time.monotonic()
        # app/game names as hints ("odpal steama", not "odpal z tima"); the wake phrase is NOT in them
        # (hinting "Hej Pitagoras" made Whisper drop the start of the sentence)
        segments, info = self.model.transcribe(
            audio, language=self._pick_language(audio), beam_size=1, vad_filter=True,
            condition_on_previous_text=False, hotwords=self.vocab or None,
        )
        text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()
        norm = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if not norm or norm in HALLUCINATIONS:
            return
        log.info("heard (%.2fs, %s): %s", time.monotonic() - t0, info.language, text)
        if self.live_wake:
            self.on_text(text, self.live_wake)
        else:
            self.on_text(text)
