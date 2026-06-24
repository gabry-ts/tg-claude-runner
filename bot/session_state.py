"""Persistence for the current Claude Code session metadata.

A single JSON file under STATE_DIR holds the session_id, cwd and model of the
last Claude conversation so the bot can `--resume` it after a restart.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("TGCR_STATE_DIR", str(Path.home() / ".tg-claude-runner")))
STATE_FILE = STATE_DIR / "state.json"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
