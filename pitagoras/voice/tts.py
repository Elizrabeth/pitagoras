"""Text-to-speech with sentence streaming and lip-sync levels.

The brain's text arrives token by token; Speaker cuts it into sentences and
speaks each one as soon as it's complete, so speech starts before the model
has finished. While audio plays, 'mouth' events (0..1) are sent ~33x/second.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Callable

import numpy as np
import sounddevice as sd

from ..config import ROOT, Cfg, resolve_path

log = logging.getLogger("tts")

SENTENCE_END = re.compile(r"(.+?[.!?…]+[\"')\]]*)(\s+|$)", re.S)


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[*_#`>\[\]]", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)  # emoji
    return re.sub(r"\s+", " ", text).strip()


# ── engines: each returns (float32 mono samples, sample_rate) ─────────
class KokoroEngine:
    def __init__(self, cfg: Cfg):
        from kokoro_onnx import Kokoro

        d = ROOT / "models" / "kokoro"
        self.k = Kokoro(str(d / "kokoro-v1.0.onnx"), str(d / "voices-v1.0.bin"))
        self.voice = cfg.get("kokoro_voice", "am_michael")
        self.speed = float(cfg.get("kokoro_speed", 1.0))
        self.lang = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "i": "it", "p": "pt-br", "h": "hi", "j": "ja", "z": "cmn"}.get(self.voice[0], "en-us")

    def synth(self, text: str):
        samples, sr = self.k.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        return np.asarray(samples, dtype=np.float32), sr


class PiperEngine:
    def __init__(self, cfg: Cfg):
        from piper import PiperVoice

        path = resolve_path(cfg.get("piper_model"))
        self.voice = PiperVoice.load(str(path))

    def synth(self, text: str):
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return np.zeros(0, np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate


class SapiEngine:
    """Built-in Windows voices (Settings > Time & language > Speech to add more)."""

    def __init__(self, cfg: Cfg):
        self.want = (cfg.get("sapi_voice") or "").lower()
        self._local = threading.local()

    def _voice(self):
        if not hasattr(self._local, "voice"):
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            v = win32com.client.Dispatch("SAPI.SpVoice")
            if self.want:
                for tok in v.GetVoices():
                    if self.want in tok.GetDescription().lower():
                        v.Voice = tok
                        break
            self._local.voice = v
        return self._local.voice

    def synth(self, text: str):
        import win32com.client

        v = self._voice()
        stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 22  # SAFT22kHz16BitMono
        stream.Format = fmt
        v.AudioOutputStream = stream
        v.Speak(text)
        data = bytes(stream.GetData())
        return np.frombuffer(data, np.int16).astype(np.float32) / 32768.0, 22050


PL_CHARS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
PL_WORDS = {"nie", "jest", "się", "to", "że", "jak", "co", "czy", "tak", "mam", "jestem", "dzięki", "cześć", "no",
            "ale", "już", "tu", "ty", "mi", "mnie", "na", "w", "z", "do", "dla", "jestes", "dobrze", "teraz", "może"}


def guess_lang(text: str) -> str:
    """'pl' or 'en' for one sentence (good enough to pick a voice)."""
    if PL_CHARS.search(text):
        return "pl"
    words = re.findall(r"[a-ząćęłńóśźż]+", text.lower())
    hits = sum(w in PL_WORDS for w in words)
    return "pl" if words and hits / len(words) > 0.15 else "en"


# id, label, gender, language ("multi" = speaks any language)
EDGE_VOICES = [
    ("pl-PL-MarekNeural", "Marek (Polish, male)", "m", "pl"),
    ("pl-PL-ZofiaNeural", "Zofia (Polish, female)", "f", "pl"),
    ("en-US-AndrewMultilingualNeural", "Andrew (multilingual, male)", "m", "multi"),
    ("en-US-AvaMultilingualNeural", "Ava (multilingual, female)", "f", "multi"),
    ("en-US-AndrewNeural", "Andrew (US, male)", "m", "en"),
    ("en-US-GuyNeural", "Guy (US, male)", "m", "en"),
    ("en-US-AvaNeural", "Ava (US, female)", "f", "en"),
    ("en-GB-RyanNeural", "Ryan (UK, male)", "m", "en"),
    ("en-GB-SoniaNeural", "Sonia (UK, female)", "f", "en"),
]
EDGE_PARTNER = {("m", "pl"): "pl-PL-MarekNeural", ("f", "pl"): "pl-PL-ZofiaNeural",
                ("m", "en"): "en-US-AndrewNeural", ("f", "en"): "en-US-AvaNeural"}


class EdgeEngine:
    """Microsoft's neural voices (the ones in Edge's Read Aloud). Free, needs internet.
    Great Polish voices; switches to a matching English voice for English sentences."""

    def __init__(self, cfg: Cfg):
        import edge_tts  # noqa: F401

        self.voice = cfg.get("edge_voice") or "pl-PL-MarekNeural"
        self.rate = cfg.get("edge_rate") or "+0%"
        self.pitch = "+0Hz"
        self.auto = bool(cfg.get("edge_auto_language", True))
        self.fixed: str | None = None  # 'pl'/'en' when the reply language is fixed

    def pick(self, text: str) -> str:
        meta = next((v for v in EDGE_VOICES if v[0] == self.voice), None)
        if not self.auto or not meta or meta[3] == "multi":
            return self.voice
        lang = self.fixed or guess_lang(text)  # Settings > Language fixes it
        return self.voice if lang == meta[3] else EDGE_PARTNER.get((meta[2], lang), self.voice)

    def synth(self, text: str):
        import asyncio
        import io

        import edge_tts
        import soundfile as sf

        async def fetch() -> bytes:
            out = bytearray()
            async for chunk in edge_tts.Communicate(text, self.pick(text), rate=self.rate, pitch=self.pitch).stream():
                if chunk["type"] == "audio":
                    out += chunk["data"]
            return bytes(out)

        data = b""
        for attempt in range(3):  # the online service sometimes drops a request: try again
            try:
                data = asyncio.run(fetch())
                if data:
                    break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise
                log.warning("edge voice hiccup (%s), retrying", type(e).__name__)
            import time as _t

            _t.sleep(0.3 * (attempt + 1))
        if not data:
            return np.zeros(0, np.float32), 24000
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        return (audio.mean(axis=1) if audio.ndim > 1 else audio).astype(np.float32), sr


class FishEngine:
    """Fish Audio (fish.audio) online voices, including voices you cloned there. Needs an API key."""

    URL = "https://api.fish.audio/v1/tts"

    def __init__(self, cfg: Cfg):
        import os

        self.key = cfg.get("fish_api_key") or os.environ.get("FISH_API_KEY", "")
        if not self.key:
            raise RuntimeError("Fish Audio needs an API key (Voice > Fish Audio setup…)")
        self.voice_id = cfg.get("fish_voice_id") or None
        self.model = cfg.get("fish_model") or "s1"

    def synth(self, text: str):
        import io

        import httpx
        import soundfile as sf

        body = {"text": text, "format": "wav", "normalize": True, "latency": "balanced"}
        if self.voice_id:
            body["reference_id"] = self.voice_id
        r = httpx.post(self.URL, json=body, timeout=60,
                       headers={"Authorization": f"Bearer {self.key}", "model": self.model})
        if r.status_code != 200:
            raise RuntimeError(f"Fish Audio error {r.status_code}: {r.text[:200]}")
        audio, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        return (audio.mean(axis=1) if audio.ndim > 1 else audio).astype(np.float32), sr


ENGINES = {"kokoro": KokoroEngine, "edge": EdgeEngine, "fish": FishEngine, "piper": PiperEngine, "sapi": SapiEngine}


def make_engine(cfg: Cfg):
    name = cfg.get("tts_engine", "kokoro")
    order = [name] + [n for n in ("kokoro", "piper", "sapi") if n != name]
    for n in order:
        try:
            eng = ENGINES[n](cfg)
            if n != name:
                log.warning("TTS '%s' unavailable, using '%s'", name, n)
            log.info("tts: %s", n)
            return eng
        except Exception as e:  # noqa: BLE001
            log.warning("TTS '%s' failed to load: %s", n, e)
    raise RuntimeError("No TTS engine could be loaded")


# ── speaker ───────────────────────────────────────────────────
KOKORO_VOICES = [
    ("am_michael", "Michael (US, male)"), ("am_adam", "Adam (US, male)"), ("am_fenrir", "Fenrir (US, male)"),
    ("am_puck", "Puck (US, male)"), ("af_heart", "Heart (US, female)"), ("af_bella", "Bella (US, female)"),
    ("af_nicole", "Nicole (US, female, soft)"), ("af_sky", "Sky (US, female)"), ("bm_george", "George (UK, male)"),
    ("bm_lewis", "Lewis (UK, male)"), ("bf_emma", "Emma (UK, female)"), ("bf_isabella", "Isabella (UK, female)"),
]


def sapi_voices() -> list[str]:
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        v = win32com.client.Dispatch("SAPI.SpVoice")
        return [t.GetDescription() for t in v.GetVoices()]
    except Exception:  # noqa: BLE001
        return []


class Speaker:
    def __init__(self, cfg: Cfg, emit: Callable[..., None]):
        self.engine = make_engine(cfg)
        self.engine_name = cfg.get("tts_engine", "kokoro")
        self.muted = bool(cfg.get("muted", False))
        self.device = cfg.get("output_device")
        self.emit = emit
        self.q: queue.Queue[tuple[int, str]] = queue.Queue()
        self.audio_q: queue.Queue[tuple[int, np.ndarray | None, int]] = queue.Queue()
        self.gen = 0  # bumps on interrupt; stale sentences are dropped
        self.buf = ""
        self.first = True  # the first sentence of a reply is spoken as early as possible
        self.pending = 0
        self.lock = threading.Lock()
        self.idle = threading.Event()
        self.idle.set()
        self.playing = False
        self.on_start: Callable[[], None] = lambda: None
        self.on_idle: Callable[[], None] = lambda: None
        self.rvc = None  # your voice (RVC model) applied on top of the TTS
        self.rvc_opts: dict = {}
        self.rvc_error: str | None = None
        self.fixed_lang: str | None = None  # set from Settings > Language
        self.set_prosody(cfg)
        self.load_rvc(cfg)
        # two stages: the next sentence is synthesized (and converted) while this one plays
        threading.Thread(target=self._synth_loop, daemon=True, name="tts").start()
        threading.Thread(target=self._play_loop, daemon=True, name="tts-play").start()

    def set_prosody(self, cfg: Cfg) -> None:
        """Speed / pitch of the *base* voice (before your RVC model) and the output volume."""
        self.speed = max(0.5, min(2.0, float(cfg.get("tts_speed", 1.0))))
        self.pitch = max(-12.0, min(12.0, float(cfg.get("tts_pitch", 0))))   # semitones
        self.volume = max(0.0, min(2.0, float(cfg.get("volume", 100)) / 100))
        self.kokoro_base_speed = float(cfg.get("kokoro_speed", 1.0))
        if self.pitch or abs(self.speed - 1) > 0.01:  # librosa compiles on first use (~2 s): do it now
            def warm():
                import librosa

                z = np.random.default_rng(0).standard_normal(8000).astype(np.float32) * 0.01
                librosa.effects.pitch_shift(z, sr=16000, n_steps=1)
                librosa.effects.time_stretch(z, rate=1.1)
            threading.Thread(target=warm, daemon=True).start()

    def _base_synth(self, text: str) -> tuple[np.ndarray, int]:
        """The engine's voice with speed/pitch applied (natively where the engine supports it)."""
        eng = self.engine
        native_speed = native_pitch = False
        if isinstance(eng, EdgeEngine):
            eng.fixed = self.fixed_lang
            eng.rate = f"{round((self.speed - 1) * 100):+d}%"
            eng.pitch = f"{round(150 * (2 ** (self.pitch / 12) - 1)):+d}Hz"  # semitones -> Hz around a ~150 Hz voice
            native_speed = native_pitch = True
        elif isinstance(eng, KokoroEngine):
            eng.speed = self.kokoro_base_speed * self.speed
            native_speed = True
        audio, sr = eng.synth(text)
        if len(audio) and (not native_speed and abs(self.speed - 1) > 0.01 or not native_pitch and self.pitch):
            import librosa

            if not native_speed and abs(self.speed - 1) > 0.01:
                audio = librosa.effects.time_stretch(audio, rate=self.speed)
            if not native_pitch and self.pitch:
                audio = librosa.effects.pitch_shift(audio, sr=sr, n_steps=self.pitch)
            audio = audio.astype(np.float32)
        return audio, sr

    def load_rvc(self, cfg: Cfg) -> None:
        """Load/unload the RVC voice model (blocking: call off the event loop)."""
        name = cfg.get("rvc_model") or ""
        self.rvc_opts = {"pitch": float(cfg.get("rvc_pitch", 0)), "index_rate": float(cfg.get("rvc_index_rate", 0.5)),
                         "protect": float(cfg.get("rvc_protect", 0.33))}
        if (self.rvc.name if self.rvc else "") == name:
            return
        old, self.rvc, self.rvc_error = self.rvc, None, None
        if old:
            old.close()
        if not name:
            return
        try:
            from .rvc import RVC

            self.rvc = RVC(name, device=cfg.get("rvc_device", "cuda"))
        except Exception as e:  # noqa: BLE001
            self.rvc_error = str(e)
            log.exception("could not load RVC voice '%s'", name)

    def reload(self, cfg: Cfg) -> None:
        """Apply new voice settings (engine, voice, mute, RVC) without restarting."""
        self.muted = bool(cfg.get("muted", False))
        name = cfg.get("tts_engine", "kokoro")
        if name != self.engine_name or name == "fish":
            self.engine = make_engine(cfg)
            self.engine_name = name
        elif isinstance(self.engine, KokoroEngine):
            self.engine.voice = cfg.get("kokoro_voice", self.engine.voice)
            self.engine.lang = {"a": "en-us", "b": "en-gb"}.get(self.engine.voice[0], "en-us")
        elif isinstance(self.engine, EdgeEngine):
            self.engine.voice = cfg.get("edge_voice") or self.engine.voice
        elif isinstance(self.engine, SapiEngine):
            self.engine.want = (cfg.get("sapi_voice") or "").lower()
            self.engine._local = threading.local()
        self.set_prosody(cfg)
        self.load_rvc(cfg)

    # text in
    def feed(self, text: str) -> None:
        self.buf += text
        while True:
            m = SENTENCE_END.match(self.buf)
            # need trailing whitespace to be sure the sentence ended (e.g. "3.5")
            if not m or not m.group(2):
                break
            self._put(m.group(1))
            self.buf = self.buf[m.end():]
        # start talking sooner: speak the reply's first clause as soon as there is a comma after it
        if self.first and len(self.buf) > 35:
            cut = self.buf.find(", ", 18)
            if cut != -1:
                self._put(self.buf[: cut + 1])
                self.buf = self.buf[cut + 2:]
        # very long clause without punctuation: cut at a comma
        if len(self.buf) > 220 and "," in self.buf[120:]:
            cut = self.buf.index(",", 120) + 1
            self._put(self.buf[:cut])
            self.buf = self.buf[cut:]

    def flush(self) -> None:
        if self.buf.strip():
            self._put(self.buf)
        self.buf = ""
        self.first = True  # the next text starts a new reply

    def say(self, text: str) -> None:
        self.feed(text + " ")
        self.flush()

    def _put(self, sentence: str) -> None:
        s = clean_for_speech(sentence)
        if self.muted or not s or not re.search(r"\w", s):
            return
        with self.lock:
            self.pending += 1
            self.idle.clear()
        self.first = False
        self.q.put((self.gen, s))

    def interrupt(self) -> None:
        with self.lock:
            self.gen += 1
            self.buf = ""

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        """Text -> audio with the current voice (+ RVC if a voice model is on)."""
        audio, sr = self._base_synth(text)
        rvc = self.rvc
        if rvc is not None and len(audio):
            try:
                audio, sr = rvc.convert(audio, sr, **self.rvc_opts)
            except Exception:  # noqa: BLE001
                log.exception("RVC conversion failed; using the plain voice")
        return audio, sr

    # audio out
    def _synth_loop(self) -> None:
        while True:
            gen, text = self.q.get()
            audio, sr = None, 0
            try:
                if gen == self.gen:
                    audio, sr = self.synth(text)
            except Exception:  # noqa: BLE001
                log.exception("tts failed for: %s", text)
            self.audio_q.put((gen, audio, sr))

    def _play_loop(self) -> None:
        while True:
            gen, audio, sr = self.audio_q.get()
            try:
                if gen == self.gen and audio is not None and len(audio):
                    self._play(audio, sr, gen)
            except Exception:  # noqa: BLE001
                log.exception("playback failed")
            finally:
                with self.lock:
                    self.pending -= 1
                    done = self.pending == 0
                if done:
                    if self.playing:
                        self.playing = False
                        self.emit("mouth", level=0.0)
                    self.idle.set()
                    self.on_idle()

    def _play(self, audio: np.ndarray, sr: int, gen: int) -> None:
        if not self.playing:
            self.playing = True
            self.on_start()
        block = int(sr * 0.03)
        audio = np.concatenate([audio, np.zeros(int(sr * 0.08), np.float32)])  # small breath between sentences
        if abs(self.volume - 1) > 0.01:  # volume is applied last, after the voice model
            audio = np.clip(audio * self.volume, -1.0, 1.0).astype(np.float32)
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", device=self.device, blocksize=block) as st:
            for i in range(0, len(audio), block):
                if gen != self.gen:
                    break
                chunk = audio[i : i + block]
                st.write(chunk.reshape(-1, 1))
                rms = float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0
                self.emit("mouth", level=round(min(1.0, rms * 5.0), 3))
