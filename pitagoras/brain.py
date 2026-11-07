"""Pluggable brains.

Every brain exposes:
    await brain.start()
    async for text_chunk in brain.stream(messages): ...
    await brain.close()
    brain.vision -> bool   (can it look at screenshots?)
    brain.label  -> str

`messages` use the OpenAI chat format; a message's content may be a list of
parts: {"type": "text", ...} and {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}.

Kinds:
    ServerBrain  - any OpenAI-compatible server (LM Studio, Ollama, llama.cpp, KoboldCpp, vLLM, …)
    GGUFBrain    - a .gguf file (+ optional mmproj for vision); runs llama-server.exe for you
    ClaudeBrain  - the Claude Code CLI (`claude -p`), uses your Claude login
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator

import httpx

from .config import ROOT, Cfg, resolve_path

log = logging.getLogger("brain")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Brain:
    vision = False
    label = "?"

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        raise NotImplementedError


class ServerBrain(Brain):
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float, max_tokens: int,
                 vision: bool = False, label: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.vision = vision
        self.label = label or model or self.base_url
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key or 'none'}"},
            timeout=httpx.Timeout(600, connect=10),
        )

    async def start(self) -> None:
        if not self.model:
            try:
                r = await self.client.get("/models")
                r.raise_for_status()
                models = r.json().get("data", [])
                self.model = models[0]["id"] if models else "local-model"
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"Could not reach the model server at {self.base_url} ({e}).") from e
            self.label = self.model
        log.info("brain: %s @ %s%s", self.model, self.base_url, " (vision)" if self.vision else "")

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # ask servers that support it (llama.cpp, vLLM, …) to skip silent "thinking"; others ignore it
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self.client.stream("POST", "/chat/completions", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="replace")[:500]
                raise RuntimeError(f"model server error {r.status_code}: {body}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                text = (choices[0].get("delta") or {}).get("content")
                if text:
                    yield text

    async def close(self) -> None:
        await self.client.aclose()


class GGUFBrain(ServerBrain):
    """Runs llama-server.exe with a .gguf (and its mmproj, for vision) as a child process."""

    def __init__(self, model_path: Path, mmproj: Path | None, gcfg: Cfg, temperature: float, max_tokens: int, label: str):
        self.gcfg = gcfg
        self.port = int(gcfg.get("port", 8089))
        super().__init__(f"http://127.0.0.1:{self.port}/v1", "none", "local", temperature, max_tokens,
                         vision=mmproj is not None, label=label)
        self.model_path, self.mmproj = model_path, mmproj
        self.proc: subprocess.Popen | None = None

    async def start(self) -> None:
        exe = resolve_path(self.gcfg.get("server_exe"))
        if not exe or not exe.exists():
            raise RuntimeError(f"llama-server not found at {exe}. Run: .\\setup.ps1 -WithLlamaCpp")
        if not self.model_path.exists():
            raise RuntimeError(f"GGUF model not found: {self.model_path}")
        args = [
            str(exe), "-m", str(self.model_path),
            "--host", "127.0.0.1", "--port", str(self.port),
            "-ngl", str(self.gcfg.get("gpu_layers", 999)),
            "-c", str(self.gcfg.get("context", 8192)),
        ]
        if self.mmproj:
            args += ["--mmproj", str(self.mmproj)]
        if not self.gcfg.get("thinking", False):
            # a talking character shouldn't "think" silently first: it adds seconds and can use up
            # the whole token budget, leaving an empty reply
            args += ["--reasoning", "off", "--reasoning-budget", "0"]
        args += [str(a) for a in self.gcfg.get("extra_args", []) or []]
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        self._log = open(logs / "llama-server.log", "w", encoding="utf-8", errors="replace")
        log.info("starting llama-server with %s%s", self.model_path.name, " + vision" if self.mmproj else "")
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)

        async with httpx.AsyncClient(timeout=2) as c:
            for _ in range(600):  # up to ~5 min for big models
                if self.proc.poll() is not None:
                    raise RuntimeError(f"llama-server exited (code {self.proc.returncode}); see logs/llama-server.log")
                try:
                    if (await c.get(f"http://127.0.0.1:{self.port}/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("llama-server did not become ready in time; see logs/llama-server.log")
        log.info("brain: %s (llama-server :%d)", self.label, self.port)

    async def close(self) -> None:
        await super().close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "_log", None):
            self._log.close()


def _claude_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for part in content:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            head, data = url.split(",", 1)
            media = head.split(";")[0].removeprefix("data:")
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return blocks


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(p["text"] if p.get("type") == "text" else "[image]" for p in content)


class ClaudeBrain(Brain):
    """Claude through the Claude Code CLI in print mode. Its own tools are turned
    off; it uses Pitagoras' tools like any other brain. One process per reply."""

    vision = True

    def __init__(self, model: str = "sonnet"):
        self.model = model
        self.label = f"Claude ({model})"
        self.exe = shutil.which("claude")

    async def start(self) -> None:
        if not self.exe:
            raise RuntimeError("Claude Code CLI not found (install it and log in with `claude`).")
        log.info("brain: Claude Code CLI, model %s", self.model)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        rest = messages[1:] if system else messages
        blocks: list[dict] = []
        if len(rest) > 1:
            transcript = "\n\n".join(f"{m['role'].upper()}: {_text_of(m['content'])}" for m in rest[:-1])
            blocks.append({"type": "text", "text": f"Conversation so far:\n{transcript}\n\n--- Latest message ---"})
        blocks += _claude_blocks(rest[-1]["content"])
        line = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"

        args = [
            self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--system-prompt", system, "--tools", "",
            "--no-session-persistence", "--strict-mcp-config", "--setting-sources", "", "--model", self.model,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(), limit=32 * 1024 * 1024, creationflags=NO_WINDOW,
        )
        try:
            proc.stdin.write(line.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("type") == "stream_event":
                    e = ev.get("event", {})
                    d = e.get("delta", {})
                    if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                        yield d["text"]
                elif ev.get("type") == "result" and ev.get("is_error"):
                    raise RuntimeError(f"Claude: {ev.get('result') or ev.get('subtype')}")
            await proc.wait()
            if proc.returncode:
                err = (await proc.stderr.read()).decode(errors="replace")[:300]
                raise RuntimeError(f"claude exited {proc.returncode}: {err}")
        finally:
            if proc.returncode is None:
                proc.kill()


def build_brain(entry: dict, cfg: Cfg) -> Brain:
    """Make a brain from a catalog entry (see brains.py)."""
    b = cfg.brain
    temp, max_tokens = float(b.get("temperature", 0.7)), int(b.get("max_tokens", 600))
    kind = entry["kind"]
    if kind == "gguf":
        return GGUFBrain(Path(entry["model_path"]), Path(entry["mmproj"]) if entry.get("mmproj") else None,
                         Cfg(entry.get("gguf") or b.gguf), temp, max_tokens, entry["label"])
    if kind == "claude":
        return ClaudeBrain(entry["model"])
    return ServerBrain(entry["base_url"], entry.get("api_key", ""), entry.get("model", ""), temp, max_tokens,
                       vision=bool(entry.get("vision")), label=entry["label"])
