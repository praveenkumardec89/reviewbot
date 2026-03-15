"""
Shared config helpers for ReviewCrew engine modules.
Centralises model resolution so every module honours the consumer's choice.
"""

import os
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-4-20250514"
_CONFIG_FILE  = Path(".reviewcrew/config.yaml")


def load_model() -> str:
    """
    Resolve which Claude model to use. Priority (highest → lowest):
      1. REVIEWCREW_MODEL env var  (CI / workflow override)
      2. model: field in .reviewcrew/config.yaml  (consumer config)
      3. Built-in default (claude-sonnet-4-20250514)
    """
    if env_model := os.environ.get("REVIEWCREW_MODEL", "").strip():
        return env_model

    if _CONFIG_FILE.exists():
        try:
            import yaml
            cfg = yaml.safe_load(_CONFIG_FILE.read_text()) or {}
            if model := cfg.get("model", "").strip():
                return model
        except Exception:
            pass

    return DEFAULT_MODEL
