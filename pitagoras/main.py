from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

from . import memory, tools
from .agent import GLANCE_PROMPT, Agent
from .watch import TAP, Watcher
from .brain import Brain, build_brain
from .brains import catalog
from .config import BRAINS_DIR, ROOT, load_config, save_setting
from .events import EventHub

log = logging.getLogger("pitagoras")

WHISPER_SIZES = [("tiny", "fastest, least accurate"), ("base", "fast"), ("small", "balanced"),
                 ("medium", "accurate"), ("large-v3-turbo", "most accurate")]
EYES_MODES = [("off", "Off"), ("ask", "Only when I ask"), ("auto:2", "Comment every ~2 min"),
              ("auto:5", "Comment every ~5 min"), ("auto:15", "Comment every ~15 min")]
LENGTH_OPTS = [("short", "Short (one sentence)"), ("medium", "Medium (1-3 sentences)"), ("long", "Long (fuller answers)")]
CHATTER_OPTS = [("quiet", "Quiet (only notable moments, ~2 min)"), ("normal", "Normal (~45 s)"),
                ("chatty", "Chatty (reacts a lot, ~20 s)")]


LANGUAGE_OPTS = [("auto", "Auto (answer in the language I use)"), ("pl", "Polski"), ("en", "English")]
EARS_LANGUAGE_OPTS = [("auto", "Polish + English (auto)"), ("pl", "Polski"), ("en", "English")]


def voice_for_language(v, lang: str) -> dict:
    """Voice settings that can speak `lang` ('pl'/'en'), keeping the current voice's gender
    and engine where possible. Returns only what has to change."""
    from .voice.tts import EDGE_PARTNER, EDGE_VOICES

    eng = v.get("tts_engine", "kokoro")
    meta = {i: (g, lng) for i, _lbl, g, lng in EDGE_VOICES}
    if eng == "edge":
        gender, vlang = meta.get(v.get("edge_voice") or "", ("m", "pl"))
    elif eng == "kokoro":
        gender, vlang = ("f" if str(v.get("kokoro_voice", "am"))[1:2] == "f" else "m"), "en"
    elif eng == "piper":
        gender, vlang = "m", "pl"
    else:  # fish speaks what it's given; sapi: unknown
        gender, vlang = "m", "multi" if eng == "fish" else "?"
    if vlang in (lang, "multi"):
        return {}
    if lang == "pl":
        return {"tts_engine": "edge", "edge_voice": EDGE_PARTNER[(gender, "pl")]}
    if eng == "edge":
        return {"edge_voice": EDGE_PARTNER[(gender, "en")]}
    return {"tts_engine": "kokoro", "kokoro_voice": "af_heart" if gender == "f" else "am_michael"}


def guess_lang_pl(cfg) -> bool:
    """Is the chosen voice a Polish one? (for the 'this is how I sound' sample line)"""
    v = cfg.voice
    eng = v.get("tts_engine", "kokoro")
    lang = (cfg.get("chat") or {}).get("language", "auto")
    if lang in ("pl", "en"):
        return lang == "pl"
    return (eng == "edge" and str(v.get("edge_voice", "pl-PL")).startswith("pl-")) or eng in ("piper", "fish")


def list_mics() -> list[str]:
    import sounddevice as sd

    api = sd.default.hostapi
    seen, out = set(), []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and d["hostapi"] == api and d["name"] not in seen:
            seen.add(d["name"])
            out.append(d["name"])
    return out


class App:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config(args.config)
        self.hub = EventHub(self.cfg.events.get("host", "127.0.0.1"), int(self.cfg.events.get("port", 8765)))
        self.brain_status = "none"
        self.brain_lock = asyncio.Lock()
        self.catalog: list[dict] = []
        self.speaker = None
        self.listener = None
        self.ears_status = "off"
        self.ears_lock = threading.Lock()
        self.last_glance = time.monotonic()
        self.stop = asyncio.Event()
        self.overlay: subprocess.Popen | None = None
        self.agent: Agent | None = None
        self.watcher = Watcher(self)
        self.rvc_loading = False

    # ── settings helpers ─────────────────────────────────────
    def ears_spec(self) -> str:
        if self.args.no_voice:
            return "off"
        v = self.cfg.voice
        return v.get("ears") or (f"whisper:{v.get('stt_model', 'small')}" if v.get("enabled", True) else "off")

    def voice_id(self) -> str:
        v = self.cfg.voice
        eng = v.get("tts_engine", "kokoro")
        if eng == "kokoro":
            return f"kokoro:{v.get('kokoro_voice', 'am_michael')}"
        if eng == "sapi":
            return f"sapi:{v.get('sapi_voice', '')}"
        if eng == "edge":
            return f"edge:{v.get('edge_voice') or 'pl-PL-MarekNeural'}"
        return eng

    def eyes_mode(self) -> str:
        return (self.cfg.get("eyes") or {}).get("mode", "ask")

    def reload_cfg(self) -> None:
        self.cfg = load_config(self.args.config)
        tools.ctx.cfg = self.cfg
        tools.ctx.extra["eyes_mode"] = self.eyes_mode()
        if self.agent:
            self.agent.cfg = self.cfg
            self.agent.wake_word = bool(self.cfg.voice.get("wake_word"))
            self.agent.follow_ups = bool(self.cfg.voice.get("follow_ups", False))
            phrase = self.cfg.voice.get("wake_phrase") or "hej pitagoras"
            if phrase.lower() != self.agent.wake_phrase:
                self.agent.set_wake_phrase(phrase)
            if self.listener is not None and hasattr(self.listener, "hotwords"):  # the live check spells it right
                self.listener.hotwords = f"{phrase.title()}, {phrase.split()[-1].title()}"
        if self.speaker:
            lang = (self.cfg.get("chat") or {}).get("language", "auto")
            self.speaker.fixed_lang = lang if lang in ("pl", "en") else None

    def t(self, pl: str, en: str) -> str:
        """A line it shows or says itself, in the chosen language (Settings > Language)."""
        return self.agent.phrase(pl, en) if getattr(self, "agent", None) else (pl if guess_lang_pl(self.cfg) else en)

    def toast(self, text: str) -> None:
        log.info("%s", text)
        self.hub.emit("toast", text=text)

    async def settings_payload(self) -> dict:
        from .eyes import monitors
        from .voice.rvc import list_models
        from .voice.tts import EDGE_VOICES, KOKORO_VOICES, sapi_voices

        self.catalog = await catalog(self.cfg)
        b = self.agent.brain if self.agent else None
        sapi = await asyncio.to_thread(sapi_voices)
        mics = await asyncio.to_thread(list_mics)
        ears = [{"id": f"whisper:{s}", "label": f"Whisper {s} ({d})", "group": "Whisper (local, GPU)"} for s, d in WHISPER_SIZES]
        ears += [{"id": "windows", "label": "Windows speech recognition", "group": "Windows"},
                 {"id": "off", "label": "Off (text only)", "group": ""}]
        voices = [{"id": f"edge:{i}", "label": lbl, "group": "Neural voices (online, Polish + English)"}
                  for i, lbl, _g, _l in EDGE_VOICES]
        voices += [{"id": f"kokoro:{k}", "label": lbl, "group": "Kokoro (local, English)"} for k, lbl in KOKORO_VOICES]
        if self.cfg.voice.get("fish_api_key"):
            voices.append({"id": "fish", "label": "Fish Audio voice" + (f" ({self.cfg.voice.get('fish_voice_id')[:12]}…)"
                                                                         if self.cfg.voice.get("fish_voice_id") else ""),
                           "group": "Fish Audio (online)"})
        voices += [{"id": f"sapi:{n}", "label": n, "group": "Windows voices"} for n in sapi]
        if (ROOT / str(self.cfg.voice.get("piper_model", ""))).exists():
            voices.append({"id": "piper", "label": "Piper voice (local, Polish)", "group": "Piper"})
        rvc_models = await asyncio.to_thread(list_models)
        sp = self.speaker
        return {
            "current": {
                "brain": self.cfg.brain.get("selected"), "brain_label": b.label if b else None,
                "brain_vision": bool(b and b.vision), "brain_status": self.brain_status,
                "ears": self.ears_spec(), "ears_status": self.ears_status,
                "mic": self.cfg.voice.get("input_device"), "eyes": self.eyes_mode(), "voice": self.voice_id(),
                "muted": bool(self.cfg.voice.get("muted")), "wake_word": bool(self.cfg.voice.get("wake_word")),
                "follow_ups": bool(self.cfg.voice.get("follow_ups", False)),
                "brains_dir": str(BRAINS_DIR),
                "length": (self.cfg.get("chat") or {}).get("length", "medium"),
                "chatter": self.watcher.chatter(), "watching": self.watcher.active,
                "memory": bool((self.cfg.get("memory") or {}).get("enabled", True)),
                "remembered": len(self.agent.history) if self.agent else 0,
                "notes": len([ln for ln in memory.notes().splitlines() if ln.strip()]),
                "rvc": self.cfg.voice.get("rvc_model") or "", "rvc_pitch": float(self.cfg.voice.get("rvc_pitch", 0)),
                "rvc_status": ("loading" if self.rvc_loading else
                               f"error: {sp.rvc_error}" if sp and sp.rvc_error else
                               "ready" if sp and sp.rvc else "off"),
                "fish_ready": bool(self.cfg.voice.get("fish_api_key")),
                "tts_speed": float(self.cfg.voice.get("tts_speed", 1.0)),
                "tts_pitch": float(self.cfg.voice.get("tts_pitch", 0)),
                "volume": float(self.cfg.voice.get("volume", 100)),
                "pc_control": bool((self.cfg.get("control") or {}).get("enabled", True)),
                "language": (self.cfg.get("chat") or {}).get("language", "auto"),
                "task_comments": bool((self.cfg.get("chat") or {}).get("task_comments", False)),
                "fact_check": bool((self.cfg.get("chat") or {}).get("fact_check", True)),
                "wake_phrase": self.cfg.voice.get("wake_phrase") or "hej pitagoras",
                "user_name": (self.cfg.get("chat") or {}).get("user_name") or "",
                "ears_language": self.cfg.voice.get("ears_language", "auto"),
                "eyes_screen": str((self.cfg.get("eyes") or {}).get("screen", "mouse")),
            },
            "options": {
                "brains": [{k: e[k] for k in ("id", "label", "group", "vision")} for e in self.catalog],
                "ears": ears, "mics": mics, "eyes": [{"id": i, "label": lbl} for i, lbl in EYES_MODES], "voices": voices,
                "length": [{"id": i, "label": lbl} for i, lbl in LENGTH_OPTS],
                "chatter": [{"id": i, "label": lbl} for i, lbl in CHATTER_OPTS],
                "rvc": [{"id": "", "label": "Off (normal voice)"}] + [{"id": n, "label": n} for n in rvc_models],
                "language": [{"id": i, "label": lbl} for i, lbl in LANGUAGE_OPTS],
                "ears_language": [{"id": i, "label": lbl} for i, lbl in EARS_LANGUAGE_OPTS],
                "eyes_screen": [{"id": "mouse", "label": "The one my mouse is on"},
                                {"id": "character", "label": "The one Pitagoras is on"}]
                               + [{"id": str(i), "label": f"Screen {i}  ({m['width']}×{m['height']}{', main' if m['primary'] else ''})",
                                   "group": "Always"} for i, m in enumerate(await asyncio.to_thread(monitors), 1)],
            },
        }

    async def push_settings(self) -> None:
        self.hub.emit("settings", **(await self.settings_payload()))

    # ── brain ────────────────────────────────────────────────
    async def load_brain(self) -> None:
        async with self.brain_lock:
            self.agent.interrupt()
            old: Brain | None = self.agent.brain
            self.agent.brain = None
            if old:
                await old.close()
            self.catalog = await catalog(self.cfg)
            sel = self.cfg.brain.get("selected")
            entry = next((e for e in self.catalog if e["id"] == sel), None)
            if entry is None:
                entry = next(iter(self.catalog), None)
                if sel:
                    self.toast(self.t(f"Nie znalazłem mózgu '{sel}'" + (f", używam {entry['label']}" if entry else ""),
                                      f"Brain '{sel}' not found" + (f", using {entry['label']}" if entry else "")))
            if entry is None:
                self.brain_status = "none"
                self.toast(self.t(f"Nie mam mózgu. Wrzuć model do {BRAINS_DIR}\\<nazwa>\\ albo włącz LM Studio/Ollamę.",
                              f"No brain found. Put a model in {BRAINS_DIR}\\<name>\\ or start LM Studio/Ollama."))
                await self.push_settings()
                return
            self.brain_status = "loading"
            self.hub.emit("state", state="loading")
            self.toast(self.t(f"Ładuję mózg: {entry['label']}…", f"Loading brain: {entry['label']}…"))
            try:
                brain = build_brain(entry, self.cfg)
                await brain.start()
            except Exception as e:  # noqa: BLE001
                self.brain_status = f"error: {e}"
                self.hub.emit("error", message=str(e))
                self.toast(self.t(f"Mózg nie wystartował: {e}", f"Brain failed: {e}"))
                self.hub.emit("state", state="idle")
                await self.push_settings()
                return
            self.agent.brain = brain
            self.brain_status = "ready"
            self.hub.emit("state", state="idle")
            self.toast(self.t(f"Mózg gotowy: {brain.label}" + (" (widzi ekran)" if brain.vision else ""),
                              f"Brain ready: {brain.label}" + (" (can see)" if brain.vision else "")))
            await self.push_settings()

    # ── ears ─────────────────────────────────────────────────
    def start_ears(self) -> None:
        """Blocking (loads models): call via asyncio.to_thread."""
        with self.ears_lock:
            if self.listener:
                self.listener.stop()
                self.listener = None
            spec = self.ears_spec()
            if spec == "off":
                self.ears_status = "off"
                return
            self.ears_status = "loading"
            try:
                if spec == "windows":
                    from .voice.windows_ears import WindowsListener

                    lst = WindowsListener(self.cfg.voice, self.agent.heard, self.hub.emit)
                else:
                    from .voice.stt import Listener

                    size = spec.split(":", 1)[1] if ":" in spec else "small"
                    self.toast(self.t(f"Ładuję uszy: Whisper {size}…", f"Loading ears: Whisper {size}…"))
                    lst = Listener(self.cfg.voice, self.agent.heard, self.hub.emit, size=size,
                                   wake_check=self.agent.wake_status, on_wake=self.agent.wake_feedback)
                lst.start()
                lst.ready.wait(20)
                if lst.error:
                    raise RuntimeError(lst.error)
            except Exception as e:  # noqa: BLE001
                self.ears_status = f"error: {e}"
                self.toast(self.t(f"Uszy nie działają: {e}", f"Ears failed: {e}"))
                return
            self.listener = lst
            self.ears_status = "ready"
            self.toast(self.t("Uszy gotowe: ", "Ears ready: ")
                       + ("Windows speech" if spec == "windows" else spec.replace(":", " ")))

    # ── eyes ─────────────────────────────────────────────────
    async def look(self, prompt: str = "Take a look at my screen. What do you think?") -> None:
        from .eyes import can_see, capture

        ok, why = can_see()
        if not ok:
            self.hub.emit("assistant_start")
            self.agent._say(self.t(
                "Nie widzę ekranu: oczy są wyłączone w ustawieniach (prawy klik na mnie > Eyes)."
                if "turned off" in why else
                "Nie widzę ekranu: ten mózg nie ma wzroku. Potrzebny jest model z wizją (np. lokalny z plikiem mmproj).",
                why.replace("You can't", "I can't").replace("your eyes", "my eyes").replace("You", "I")))
            self.hub.emit("assistant_end")
            if self.speaker:
                self.speaker.flush()
            return
        if prompt == "Take a look at my screen. What do you think?":
            prompt = self.t("Zerknij na mój ekran. Co o tym myślisz?", prompt)
        img = await asyncio.to_thread(capture, tools.ctx.extra.get("char_pos"))
        await self.agent.submit(prompt, image=img)

    async def eyes_loop(self) -> None:
        from .eyes import can_see, capture

        while True:
            await asyncio.sleep(10)
            mode = self.eyes_mode()
            if not mode.startswith("auto:") or self.watcher.active or self.agent.busy or not can_see()[0]:
                continue
            minutes = float(mode.split(":")[1])
            now = time.monotonic()
            if now - max(self.last_glance, self.agent.last_activity, self.agent.last_reply) < minutes * 60:
                continue
            self.last_glance = now
            img = await asyncio.to_thread(capture, tools.ctx.extra.get("char_pos"))
            await self.agent.submit(GLANCE_PROMPT, image=img, auto=True)

    async def set_watch(self, on: bool) -> None:
        self.watcher.set(on)
        if on and not self.cfg.music_sense.get("enabled", True):
            self.toast(self.t("Oglądam z tobą (music sense jest wyłączony w config.yaml, więc nie słyszę filmu)",
                              "Watching along (music sense is off in config.yaml, so I can't hear the video)"))
        else:
            self.toast(self.t("Oglądam z tobą 📺" if on else "Koniec wspólnego oglądania",
                              "Watching along with you 📺" if on else "Stopped watching along"))
        await self.push_settings()

    async def set_language(self, lang: str) -> None:
        """Reply language: changes the prompt and, if needed, switches to a voice that speaks it."""
        save_setting("chat", "language", lang)
        changes = voice_for_language(self.cfg.voice, lang) if lang in ("pl", "en") else {}
        for k, val in changes.items():
            save_setting("voice", k, val)
        self.reload_cfg()
        name = dict(LANGUAGE_OPTS).get(lang, lang)
        if changes and self.speaker:
            try:
                await asyncio.to_thread(self.speaker.reload, self.cfg.voice)
            except Exception as e:  # noqa: BLE001
                self.toast(self.t(f"Głos nie działa: {e}", f"Voice failed: {e}"))
            self.toast(self.t(f"Język: {name} (głos: {self.voice_id().split(':', 1)[-1]})",
                              f"Language: {name} (voice: {self.voice_id().split(':', 1)[-1]})"))
        else:
            self.toast(self.t(f"Język: {name}", f"Language: {name}"))
        if self.speaker and lang in ("pl", "en"):
            self.speaker.say("Dobra, od teraz mówię po polsku." if lang == "pl" else "Okay, I'll speak English from now on.")
        await self.push_settings()

    async def set_voice_model(self, key: str, value, data: dict) -> None:
        """Your own voice: RVC model on/off and pitch, or Fish Audio setup."""
        if key == "rvc":
            save_setting("voice", "rvc_model", value or "")
        elif key == "rvc_pitch":
            save_setting("voice", "rvc_pitch", float(value))
        elif key == "fish":
            save_setting("voice", "fish_api_key", (data.get("api_key") or "").strip())
            save_setting("voice", "fish_voice_id", (data.get("voice_id") or "").strip())
            save_setting("voice", "tts_engine", "fish")
        self.reload_cfg()
        if not self.speaker:
            await self.push_settings()
            return
        if key == "rvc" and value:
            self.rvc_loading = True
            self.toast(self.t(f"Ładuję model głosu '{value}'… (za pierwszym razem pobiera ~550 MB)",
                              f"Loading voice model '{value}'… (the first time downloads ~550 MB)"))
            await self.push_settings()
        try:
            await asyncio.to_thread(self.speaker.reload, self.cfg.voice)
        except Exception as e:  # noqa: BLE001
            self.toast(self.t(f"Głos nie działa: {e}", f"Voice failed: {e}"))
        finally:
            self.rvc_loading = False
        sp = self.speaker
        if key == "rvc":
            if sp.rvc_error:
                self.toast(self.t(f"Model głosu nie działa: {sp.rvc_error[:150]}", f"Voice model failed: {sp.rvc_error[:150]}"))
            else:
                self.toast(self.t(f"Mówię modelem głosu '{value}'" if value else "Model głosu wyłączony",
                                  f"Speaking with voice model '{value}'" if value else "Voice model off"))
        if not (key == "rvc" and sp.rvc_error):
            sp.say(self.t("Cześć! Tak teraz brzmię.", "Hi! This is how I sound now."))
        await self.push_settings()

    # ── commands from the character ──────────────────────────
    async def on_set(self, msg: dict) -> None:
        key, value = msg.get("key"), msg.get("value")
        log.info("setting %s = %s", key, value)
        if key == "watch":
            await self.set_watch(bool(value))
            return
        if key in ("rvc", "rvc_pitch", "fish"):
            await self.set_voice_model(key, value, msg.get("data") or {})
            return
        if key == "language":
            await self.set_language(value)
            return
        if key in ("wake_phrase", "user_name"):
            value = " ".join(str(value or "").split())
            if key == "wake_phrase":
                if len(value.split()) < 1:
                    self.toast(self.t("Fraza budząca nie może być pusta.", "The wake phrase can't be empty."))
                    return
                save_setting("voice", "wake_phrase", value.lower())
                self.toast(self.t(f"Teraz reaguję na „{value}”", f"Now I listen for “{value}”"))
            else:
                save_setting("chat", "user_name", value)
                self.toast(self.t(f"Okej, {value}!" if value else "Okej, nie będę cię nazywał po imieniu.",
                                  f"Okay, {value}!" if value else "Okay, I won't use a name for you."))
            self.reload_cfg()
            await self.push_settings()
            return
        if key == "ears_language":
            save_setting("voice", "ears_language", value)
            self.reload_cfg()
            await asyncio.to_thread(self.start_ears)
            await self.push_settings()
            return
        simple = {"length": ("chat", "length"), "chatter": ("watch", "chatter"), "memory": ("memory", "enabled"),
                  "pc_control": ("control", "enabled"), "task_comments": ("chat", "task_comments"),
                  "fact_check": ("chat", "fact_check"), "eyes_screen": ("eyes", "screen")}
        if key in simple:
            save_setting(*simple[key], bool(value) if key in ("memory", "pc_control", "task_comments", "fact_check") else value)
            self.reload_cfg()
            if key == "memory" and value:
                self.agent.save()
            await self.push_settings()
            return
        if key == "brain":
            if value == "custom" and msg.get("data"):
                save_setting("brain", "custom", {**(self.cfg.brain.get("custom") or {}), **msg["data"]})
            save_setting("brain", "selected", value)
            self.reload_cfg()
            asyncio.create_task(self.load_brain())
            await self.push_settings()
            return
        if key == "ears":
            save_setting("voice", "ears", value)
        elif key == "mic":
            save_setting("voice", "input_device", value)
        elif key == "eyes":
            save_setting("eyes", "mode", value)
            self.last_glance = time.monotonic()
        elif key == "voice":
            if value.startswith("kokoro:"):
                save_setting("voice", "tts_engine", "kokoro")
                save_setting("voice", "kokoro_voice", value.split(":", 1)[1])
            elif value.startswith("sapi:"):
                save_setting("voice", "tts_engine", "sapi")
                save_setting("voice", "sapi_voice", value.split(":", 1)[1])
            elif value.startswith("edge:"):
                save_setting("voice", "tts_engine", "edge")
                save_setting("voice", "edge_voice", value.split(":", 1)[1])
            else:
                save_setting("voice", "tts_engine", value)
        elif key in ("muted", "wake_word", "follow_ups"):
            save_setting("voice", key, bool(value))
        elif key in ("tts_speed", "tts_pitch", "volume"):
            save_setting("voice", key, float(value))
        else:
            return
        self.reload_cfg()
        if key in ("ears", "mic"):
            await asyncio.to_thread(self.start_ears)
        elif key in ("voice", "muted", "tts_speed", "tts_pitch", "volume") and self.speaker:
            try:
                await asyncio.to_thread(self.speaker.reload, self.cfg.voice)
                if key != "muted":
                    self.speaker.say(self.t("Cześć! Tak teraz brzmię.", "How do I sound now?"))
            except Exception as e:  # noqa: BLE001
                self.toast(self.t(f"Głos nie działa: {e}", f"Voice failed: {e}"))
        await self.push_settings()

    def register_handlers(self) -> None:
        h = self.hub

        async def say(m):
            await self.agent.submit(m.get("text", ""))

        async def interrupt(_m):
            self.agent.interrupt()

        async def speak(m):
            if self.speaker:
                self.speaker.say(m.get("text", ""))

        async def get_settings(_m):
            await self.push_settings()

        async def look(m):
            await self.look(m.get("prompt") or "Take a look at my screen. What do you think?")

        async def where(m):
            tools.ctx.extra["char_pos"] = (int(m.get("x", 0)), int(m.get("y", 0)))

        async def open_brains(_m):
            BRAINS_DIR.mkdir(exist_ok=True)
            os.startfile(BRAINS_DIR)

        async def clear(m):
            self.agent.interrupt()
            self.agent.history.clear()
            self.agent.restored_at = None
            if m.get("all"):
                memory.clear_all()
                self.toast(self.t("Wyczyściłem całą pamięć: rozmowę i zapisane notatki.",
                                  "All memory cleared: conversation and saved notes."))
            else:
                memory.clear_conversation()
                self.toast(self.t("Zapomniałem tę rozmowę (notatki zostały).", "Forgot this conversation (saved notes kept)."))
            await self.push_settings()

        async def edit_personality(_m):
            memory.personality(self.cfg.get("persona"))  # creates the file if missing
            memory.edit_file(memory.PERSONALITY)
            self.toast(self.t("Edytujesz personality.txt: zapisz, a użyję go od następnej odpowiedzi.",
                              "Editing personality.txt: save it and I'll use it from my next reply."))

        async def reset_personality(_m):
            memory.reset_personality(self.cfg.get("persona"))
            self.toast(self.t("Przywróciłem domyślną osobowość.", "Personality reset to the default."))

        async def edit_notes(_m):
            memory.edit_file(memory.NOTES)
            self.toast(self.t("Edytujesz notatki pamięci: jeden fakt w linijce.", "Editing memory notes: one fact per line."))

        async def edit_words(_m):
            from .voice.stt import WORDS_HELP, words_file

            f = words_file()
            if not f.exists():
                f.write_text(WORDS_HELP + "Xayah\nflashnął\n", encoding="utf-8")
            memory.edit_file(f)
            self.toast(self.t("Moje słowa: jedno w linijce. Uszy łapią je od razu.",
                              "My words: one per line. The ears pick them up right away."))

        async def open_voices(_m):
            from .voice.rvc import VOICES_DIR

            VOICES_DIR.mkdir(exist_ok=True)
            os.startfile(VOICES_DIR)

        async def open_memory(_m):
            memory.MEMORY_DIR.mkdir(exist_ok=True)
            os.startfile(memory.MEMORY_DIR)

        async def quit_(_m):
            self.stop.set()

        for name, fn in [("say", say), ("interrupt", interrupt), ("speak", speak), ("get_settings", get_settings),
                         ("set", self.on_set), ("look", look), ("where", where), ("open_brains_folder", open_brains),
                         ("clear_memory", clear), ("edit_personality", edit_personality),
                         ("reset_personality", reset_personality), ("edit_notes", edit_notes), ("edit_words", edit_words),
                         ("open_memory_folder", open_memory), ("open_voices_folder", open_voices), ("quit", quit_)]:
            h.on(name, fn)

    # ── lifecycle ────────────────────────────────────────────
    def start_overlay(self) -> None:
        if self.args.no_overlay or not (self.cfg.get("overlay") or {}).get("enabled", True):
            return
        self.overlay = subprocess.Popen([sys.executable, "-m", "pitagoras.overlay", "--port", str(self.hub.port)], cwd=ROOT)

    async def run(self) -> None:
        await self.hub.start()
        tools.ctx.hub, tools.ctx.cfg = self.hub, self.cfg
        tools.ctx.extra["eyes_mode"] = self.eyes_mode()
        tools.load_all()
        self.hub.emit("state", state="loading")
        self.start_overlay()

        if not self.args.no_voice:
            from .voice.tts import Speaker

            self.speaker = await asyncio.to_thread(Speaker, self.cfg.voice, self.hub.emit)
            self.reload_cfg()  # applies the fixed language to the speaker

        self.agent = Agent(self.cfg, None, self.hub, self.speaker)
        self.agent.restore()
        memory.personality(self.cfg.get("persona"))  # create personality.txt on first run
        tools.ctx.extra["agent"] = self.agent
        tools.ctx.extra["app"] = self
        self.register_handlers()

        from . import apps

        threading.Thread(target=apps.build_index, daemon=True, name="apps").start()

        # One thing at a time: the ears are off from the moment a request starts until it is
        # completely finished (thinking, tools and speaking), so nothing can cut in.
        def ears_pause():
            if self.listener:
                self.listener.pause()

        def ears_resume():
            turn = self.agent.turn
            busy = turn is not None and not turn.done() and turn is not asyncio.current_task()
            if self.listener and not busy and (self.speaker is None or self.speaker.idle.is_set()):
                self.listener.resume()

        self.agent.on_turn = lambda active: ears_pause() if active else ears_resume()
        self.agent.on_quit = self.stop.set  # "hej pitagoras, wyłącz się"

        if self.speaker:
            def on_start():
                self.hub.emit("state", state="speaking")
                ears_pause()

            def on_idle():
                if not self.agent.busy:
                    self.hub.emit("state", state="idle")
                loop.call_soon_threadsafe(ears_resume)  # (checked on the event loop: is the turn done?)

            loop = asyncio.get_running_loop()
            self.speaker.on_start, self.speaker.on_idle = on_start, on_idle

        bg = [asyncio.create_task(self.eyes_loop()), asyncio.create_task(self.watcher.loop())]
        if self.cfg.music_sense.get("enabled", True):
            from .music_sense import MusicSense, watch_now_playing

            def np_emit(type_, **data):
                self.watcher.now_playing = data
                self.hub.emit(type_, **data)

            MusicSense(self.hub.emit, lambda: bool(self.speaker and self.speaker.playing), TAP).start()
            bg.append(asyncio.create_task(watch_now_playing(np_emit, float(self.cfg.music_sense.get("now_playing_poll_seconds", 2)))))

        await asyncio.gather(self.load_brain(), asyncio.to_thread(self.start_ears))
        self.hub.emit("hello", name=self.cfg.get("name", "Pitagoras"), tools=list(tools.TOOLS))
        print(f"\n{self.cfg.get('name', 'Pitagoras')} is ready. Talk to it, click it, or type here (Ctrl+C to quit).\n")

        loop = asyncio.get_running_loop()

        def stdin_reader():
            try:
                for line in sys.stdin:
                    asyncio.run_coroutine_threadsafe(self.agent.submit(line), loop)
            except Exception:  # noqa: BLE001  (no console)
                pass

        threading.Thread(target=stdin_reader, daemon=True).start()

        try:
            await self.stop.wait()
        finally:
            for t in bg:
                t.cancel()
            if self.listener:
                self.listener.stop()
            if self.agent.brain:
                await self.agent.brain.close()
            if self.overlay and self.overlay.poll() is None:
                self.overlay.terminate()
            await self.hub.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Pitagoras desktop assistant")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--no-voice", action="store_true", help="text only (no mic, no speech)")
    ap.add_argument("--no-overlay", action="store_true", help="don't show the floating character")
    ap.add_argument("--list-devices", action="store_true", help="show audio devices and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if sys.platform == "win32" and sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)-6s %(message)s",
        datefmt="%H:%M:%S",
    )
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    for noisy in ("httpx", "httpx2", "httpcore", "websockets", "faster_whisper", "primp", "ddgs", "phonemizer", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    try:
        asyncio.run(App(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
