"""Tiny configuration loader.

Reads a `config.env` file (KEY=VALUE, '#' comments) from the project root and
injects any values into the process environment *without overriding* variables
that are already set. This lets you keep secrets (API key, SMTP password) in one
gitignored file on the server, while still allowing real environment variables
to take precedence — handy for systemd/cron overrides.
"""

from __future__ import annotations

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Config files to load, in order. Later files do not override earlier ones.
CONFIG_FILES = ("config.env", ".env")


def load_config() -> None:
    """Load KEY=VALUE pairs from the config file(s) into os.environ.

    Existing non-empty environment variables win, so you can override any
    setting at runtime. A variable that is set but EMPTY (e.g. an inherited
    `ANTHROPIC_API_KEY=`) does not count, so the config.env value is used.
    Called once at startup.
    """

    for name in CONFIG_FILES:
        path = os.path.join(PROJECT_ROOT, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and not os.environ.get(key, "").strip():
                    os.environ[key] = value


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default
