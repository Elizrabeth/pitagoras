"""Listens to what the PC is playing (WASAPI loopback) so the character can vibe:
emits 'music' level events (~20/s) with bass/treble/beat info and a BPM guess,
plus 'now_playing' when the song changes."""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger("music")


class MusicSense(threading.Thread):
    def __init__(self, emit: Callable[..., None], is_speaking: Callable[[], bool], tap=None):
        super().__init__(daemon=True, name="music")
        self.emit = emit
        self.is_speaking = is_speaking
        self.tap = tap

    def run(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            log.warning("PyAudioWPatch not installed; music sense off")
            return
        while True:
            try:
                self._listen(pyaudio)
            except Exception as e:  # noqa: BLE001  (device changed, etc.)
                log.warning("loopback capture stopped (%s); retrying", e)
                time.sleep(3)

    def _listen(self, pyaudio) -> None:
        p = pyaudio.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            dev = speakers
            if not speakers.get("isLoopbackDevice"):
                dev = next((lb for lb in p.get_loopback_device_info_generator() if speakers["name"] in lb["name"]), None)
            if dev is None:
                raise RuntimeError("no loopback device for default speakers")
            sr = int(dev["defaultSampleRate"])
            ch = max(1, int(dev["maxInputChannels"]))
            n = 1024
            stream = p.open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True, input_device_index=dev["index"], frames_per_buffer=n)
            log.info("music sense on '%s'", dev["name"])
            self._analyse(stream, sr, ch, n, speakers["name"], p)
        finally:
            p.terminate()

    def _analyse(self, stream, sr, ch, n, speaker_name, p) -> None:
        """Per block (~21 ms): loudness and bass compared to the song's own long-term
        average (so quiet parts read as quiet), and bass kicks as beats with a strength."""
        freqs = np.fft.rfftfreq(n, 1 / sr)
        bass_band = (freqs >= 40) & (freqs < 160)
        treble_band = freqs >= 4000
        hat_band = (freqs >= 6000) & (freqs < 16000)   # hi-hats, cymbals, claps' sizzle
        mid_band = (freqs >= 200) & (freqs < 4000)     # vocals, instruments
        kick_band = (freqs >= 35) & (freqs < 110)      # kick drums (below most voices)
        window = np.hanning(n)
        hist: collections.deque[float] = collections.deque(maxlen=int(sr / n * 1.2))  # ~1.2 s of bass
        beats: collections.deque[float] = collections.deque(maxlen=12)
        last_beat = last_emit = 0.0
        loud_avg, bass_avg = 0.02, 1.0   # long-term (~20 s) averages while sound is playing
        level_s = bass_s = 0.0           # short smoothing for the emitted values
        n_avg = 0
        kick_avg = None                  # how hard this song's kicks usually hit
        hi_hist: collections.deque[float] = collections.deque(maxlen=int(sr / n * 0.6))
        hi_avg, hat_avg, prev_hi, last_hat = 1e-3, None, 0.0, 0.0
        level_slow, last_drop = 0.0, 0.0  # ~3 s loudness, to spot drops
        last_check = time.monotonic()

        while True:
            data = stream.read(n, exception_on_overflow=False)
            x = np.frombuffer(data, np.float32).reshape(-1, ch).mean(axis=1)
            if len(x) < n:
                continue
            now = time.monotonic()

            # Follow default-device changes (headphones plugged in, etc.)
            if now - last_check > 5:
                last_check = now
                wasapi = p.get_host_api_info_by_type(__import__("pyaudiowpatch").paWASAPI)
                if p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"] != speaker_name:
                    stream.close()
                    return

            if self.is_speaking():  # don't vibe to our own voice
                continue
            if self.tap is not None and self.tap.enabled:  # watch-along listens to the video
                self.tap.push(x, sr)

            spec = np.abs(np.fft.rfft(x * window))
            bass = float(spec[bass_band].mean())
            treble = float(spec[treble_band].mean())
            rms = float(np.sqrt(np.mean(x * x)))
            silent = rms < 2e-4
            if not silent:
                n_avg = min(n_avg + 1, 1000)  # plain mean at first, then a ~20 s moving average
                loud_avg += (rms - loud_avg) / n_avg
                bass_avg += (bass - bass_avg) / n_avg
            # 0.5 ~ the song's usual loudness, 1.0 ~ twice as loud; quiet parts drop toward 0
            level = 0.0 if silent else min(1.0, rms / (loud_avg * 2 + 1e-6))
            bass_level = 0.0 if silent else min(1.0, bass / (bass_avg * 2 + 1e-6))
            level_s += (level - level_s) * (0.5 if level > level_s else 0.15)
            bass_s += (bass_level - bass_s) * (0.5 if bass_level > bass_s else 0.15)

            beat, strength = False, 0.0
            kick = float(spec[kick_band].mean())
            if len(hist) > 10:
                mean = float(np.mean(hist))
                ratio = kick / (mean + 1e-9)
                # a kick: the sub-bass jumps above its recent average, and the music isn't in a quiet part
                bassy = kick > float(spec[mid_band].mean()) * 0.8  # a real kick dominates the low end
                recent = list(hist)[-6:]                              # ~130 ms
                punchy = kick > min(recent) * 2.2                     # snaps up (a held low note doesn't)
                if (ratio > 1.4 and bassy and punchy and bass_level > 0.3 and level > 0.2
                        and now - last_beat > 0.25):
                    beat = True
                    # compare with this song's typical kick: soft kicks in a breakdown stay small
                    kick_avg = kick if kick_avg is None else kick_avg + (kick - kick_avg) * 0.04
                    strength = min(1.0, kick / (kick_avg * 1.15 + 1e-9)) ** 1.5 * min(1.0, level * 1.6)
                    strength = round(strength, 3)
                    if last_beat and now - last_beat < 2:
                        beats.append(now - last_beat)
                    last_beat = now
            hist.append(kick)
            if now - last_beat > 4:
                beats.clear()  # the beat stopped: forget the old tempo
            kind = "kick" if beat else ""

            # hi-hats: a sharp jump in the high band (soft vocals/sibilants are too quiet or too smooth)
            hi = float(spec[hat_band].mean())
            if not silent:
                hi_avg += (hi - hi_avg) / max(n_avg, 1)
            if len(hi_hist) > 8 and not beat:
                hmean = float(np.mean(hi_hist))
                bright = hi > float(spec.mean()) * 0.3  # real sizzle, not a voice's faint overtones
                if (bright and hi > hmean * 1.8 and hi > prev_hi * 1.4 and hi > hi_avg * 0.6
                        and level > 0.25 and now - last_hat > 0.1):
                    hat_avg = hi if hat_avg is None else hat_avg + (hi - hat_avg) * 0.04
                    s = min(1.0, hi / (hat_avg * 1.15 + 1e-9)) ** 1.5 * min(1.0, level * 1.6)
                    if s > 0.12:
                        beat, kind, strength, last_hat = True, "hat", round(s, 3), now
            hi_hist.append(hi)
            prev_hi = hi

            # drop: the music suddenly gets much louder than the last few seconds
            level_slow += (level_s - level_slow) * 0.007
            if level_s > 0.55 and level_s > level_slow * 1.6 and now - last_drop > 4 and n_avg > 100:
                beat, kind, strength, last_drop = True, "drop", 1.0, now

            if now - last_emit > 0.05 or beat:
                last_emit = now
                bpm = None
                if len(beats) >= 4:
                    ibi = float(np.median(beats))
                    bpm = 60 / ibi
                    while bpm < 80:
                        bpm *= 2
                    while bpm > 180:
                        bpm /= 2
                    bpm = round(bpm)
                self.emit(
                    "music",
                    level=round(level_s, 3),
                    bass=round(bass_s, 3),
                    treble=round(min(1.0, treble / (bass + treble + 1e-6) * 4), 3),
                    beat=beat,
                    kind=kind,          # kick / hat / drop
                    strength=strength,
                    bpm=bpm,
                )


async def watch_now_playing(emit: Callable[..., None], interval: float = 2.0) -> None:
    from .tools.media import now_playing

    last = None
    while True:
        try:
            np_ = await now_playing()
            key = (np_ or {}).get("title"), (np_ or {}).get("artist"), (np_ or {}).get("status")
            if key != last:
                last = key
                emit("now_playing", **(np_ or {"title": "", "artist": "", "album": "", "status": "stopped", "app": ""}))
        except Exception as e:  # noqa: BLE001
            log.debug("now playing poll failed: %s", e)
        await asyncio.sleep(interval)
