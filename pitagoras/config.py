"""config.yaml holds the defaults (with comments). Choices made in the
character's menu are saved to settings.yaml, which overrides config.yaml."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "settings.yaml"
BRAINS_DIR = ROOT / "brains"


class Cfg(dict):
    """dict with attribute access: cfg.voice.stt_model"""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Cfg(value) if isinstance(value, dict) and not isinstance(value, Cfg) else value


def resolve_path(p: str | Path | None) -> Path | None:
    """Relative paths in the config are relative to the project folder."""
    if not p:
        return None
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | Path | None = None) -> Cfg:
    data = _read(Path(path) if path else ROOT / "config.yaml")
    deep_merge(data, _read(SETTINGS))
    return Cfg(data)


def save_setting(section: str, key: str, value: Any) -> None:
    """Persist one menu choice to settings.yaml."""
    data = _read(SETTINGS)
    data.setdefault(section, {})[key] = value
    with open(SETTINGS, "w", encoding="utf-8") as f:
        f.write("# Saved from Pitagoras' menu. Overrides config.yaml. Delete to reset.\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=True)
