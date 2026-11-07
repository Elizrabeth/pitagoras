"""Ears using Windows' built-in speech recognition (WinRT, the same engine as
Windows dictation). No GPU, no model download. Needs Settings > Privacy &
security > Speech > "Online speech recognition" ON for free dictation."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

log = logging.getLogger("ears")


class WindowsListener(threading.Thread):
    def __init__(self, cfg, on_text: Callable[[str], None], emit: Callable[..., None]):
        super().__init__(daemon=True, name="windows-ears")
        self.on_text, self.emit = on_text, emit
        lang = cfg.get("ears_language") if cfg.get("ears_language") in ("pl", "en") else cfg.get("language")
        self.language = {"pl": "pl-PL", "en": "en-US"}.get(lang, lang)  # Windows wants a full tag
        self.paused = threading.Event()
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.error: str | None = None

    def pause(self) -> None:
        self.paused.set()

    def resume(self, delay: float = 0.25) -> None:
        threading.Timer(delay, self.paused.clear).start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        from winrt.windows.media.speechrecognition import (
            SpeechRecognitionResultStatus,
            SpeechRecognizer,
        )

        try:
            if self.language:
                from winrt.windows.globalization import Language

                rec = SpeechRecognizer(Language(self.language))
            else:
                rec = SpeechRecognizer()
            res = await rec.compile_constraints_async()
            if res.status != SpeechRecognitionResultStatus.SUCCESS:
                raise RuntimeError(f"compile failed: {res.status}")
            session = rec.continuous_recognition_session

            def on_result(_s, args):
                text = (args.result.text or "").strip()
                self.emit("user_speaking", active=False)
                if text and not self.paused.is_set():
                    log.info("heard: %s", text)
                    self.on_text(text)

            def on_hypothesis(_s, _args):
                if not self.paused.is_set():
                    self.emit("user_speaking", active=True)

            session.add_result_generated(on_result)
            rec.add_hypothesis_generated(on_hypothesis)
            await session.start_async()
            log.info("ears: Windows speech recognition (%s)", rec.current_language.display_name)
        except Exception as e:  # noqa: BLE001
            self.error = (f"Windows speech recognition failed: {e}. Turn on Settings > Privacy & security > "
                          "Speech > Online speech recognition, or use Whisper ears.")
            log.error(self.error)
            self.ready.set()
            return
        self.ready.set()
        while not self._stop.is_set():
            await asyncio.sleep(0.2)
        try:
            await session.stop_async()
        except Exception:  # noqa: BLE001
            pass
        rec.close()
