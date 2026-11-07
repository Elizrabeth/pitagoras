"""Finds every brain Pitagoras can use.

Local brains live in  brains/<name>/ :
    brains/
      Gemma-4-E4B/
        model-Q4_K_M.gguf          <- the model (the biggest non-mmproj .gguf is used)
        mmproj-....gguf            <- optional: vision ("eyes") projector
        brain.yaml                 <- optional: context: 16384, gpu_layers: 999, extra_args: [...]

Plus models offered by LM Studio and Ollama (if they're running), the Claude
Code CLI (if installed) and a custom OpenAI-compatible server from settings.
Brain ids look like  local:<folder>  lmstudio:<model>  ollama:<model>  claude:<model>  custom
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from urllib.parse import urlsplit

import httpx
import yaml

from .config import BRAINS_DIR, Cfg

log = logging.getLogger("brains")

VISION_HINT = re.compile(r"vision|llava|-vl\b|vl-|gemma-?[34]|qwen2\.5-?vl|qwen3-?vl|minicpm-v|pixtral|moondream|internvl|mistral-small-3", re.I)
CLAUDE_MODELS = [("haiku", "Haiku - fastest"), ("sonnet", "Sonnet - balanced"), ("opus", "Opus - smartest")]


def scan_local() -> list[dict]:
    BRAINS_DIR.mkdir(exist_ok=True)
    out = []
    for d in sorted(p for p in BRAINS_DIR.iterdir() if p.is_dir()):
        ggufs = [f for f in d.glob("*.gguf")]
        models = sorted((f for f in ggufs if "mmproj" not in f.name.lower()), key=lambda f: f.stat().st_size, reverse=True)
        if not models:
            continue
        mmproj = next((f for f in ggufs if "mmproj" in f.name.lower()), None)
        extra = {}
        if (d / "brain.yaml").exists():
            extra = yaml.safe_load((d / "brain.yaml").read_text(encoding="utf-8")) or {}
        out.append({
            "id": f"local:{d.name}", "group": "Local (brains folder)", "label": d.name, "kind": "gguf",
            "model_path": str(models[0]), "mmproj": str(mmproj) if mmproj else None, "vision": mmproj is not None,
            "overrides": extra,
        })
    return out


def _root(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


async def scan_lmstudio(c: httpx.AsyncClient, base_url: str) -> list[dict]:
    try:
        r = await c.get(_root(base_url) + "/api/v0/models")
        r.raise_for_status()
        models = r.json().get("data", [])
    except Exception:  # noqa: BLE001  (not running / old version)
        return []
    return [{
        "id": f"lmstudio:{m['id']}", "group": "LM Studio", "label": m["id"], "kind": "server",
        "base_url": base_url, "model": m["id"], "vision": m.get("type") == "vlm",
    } for m in models if m.get("type") in ("llm", "vlm")]


async def scan_ollama(c: httpx.AsyncClient, base_url: str) -> list[dict]:
    root = _root(base_url)
    try:
        r = await c.get(root + "/api/tags")
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return []

    async def caps(name):
        try:
            s = await c.post(root + "/api/show", json={"model": name})
            return "vision" in (s.json().get("capabilities") or [])
        except Exception:  # noqa: BLE001
            return bool(VISION_HINT.search(name))

    vis = await asyncio.gather(*(caps(n) for n in names))
    return [{
        "id": f"ollama:{n}", "group": "Ollama", "label": n, "kind": "server",
        "base_url": base_url, "model": n, "vision": v,
    } for n, v in zip(names, vis)]


def scan_claude() -> list[dict]:
    if not shutil.which("claude"):
        return []
    return [{"id": f"claude:{m}", "group": "Claude Code", "label": f"Claude {label}", "kind": "claude", "model": m, "vision": True}
            for m, label in CLAUDE_MODELS]


def custom_entry(cfg: Cfg) -> list[dict]:
    cs = cfg.brain.get("custom") or {}
    if not cs.get("base_url"):
        return []
    return [{
        "id": "custom", "group": "Custom server", "label": cs.get("model") or cs["base_url"], "kind": "server",
        "base_url": cs["base_url"], "api_key": cs.get("api_key", ""), "model": cs.get("model", ""),
        "vision": bool(cs.get("vision", VISION_HINT.search(cs.get("model", "")))),
    }]


async def catalog(cfg: Cfg) -> list[dict]:
    b = cfg.brain
    async with httpx.AsyncClient(timeout=1.5) as c:
        lms, oll = await asyncio.gather(
            scan_lmstudio(c, b.get("lmstudio_url", "http://localhost:1234/v1")),
            scan_ollama(c, b.get("ollama_url", "http://localhost:11434/v1")),
        )
    entries = scan_local() + lms + oll + scan_claude() + custom_entry(cfg)
    for e in entries:
        if e["kind"] == "gguf":
            g = dict(b.get("gguf") or {})
            g.update(e.pop("overrides", {}) or {})
            e["gguf"] = g
    return entries
