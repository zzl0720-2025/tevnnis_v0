"""YAML config loader — reads a config file and validates it against StrategyConfig."""

from __future__ import annotations

from pathlib import Path

import yaml

from tevnnis_core.config import StrategyConfig


def load_config(path: str | Path) -> StrategyConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return StrategyConfig.model_validate(data)
