#!/usr/bin/env python3
"""SessionStart hook for Claude Code.

Standalone — invoked directly by Claude via absolute path, so it must not
depend on `bot.*` being importable. Mirrors the SESSION_MAP_FILE location
defined in bot.session_state.

Stdin payload (from Claude Code):
  {"session_id": "...", "cwd": "...", "hook_event_name": "SessionStart"}
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(os.environ.get("TGCR_STATE_DIR", str(Path.home() / ".tg-claude-runner")))
SESSION_MAP_FILE = STATE_DIR / "session_map.json"


def atomic_write(path: Path, data: str) -> None:
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        sys.stderr.write(f"hook_runner: bad stdin: {e}\n")
        return 1

    data = {
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "hook_event_name": payload.get("hook_event_name"),
    }
    try:
        atomic_write(SESSION_MAP_FILE, json.dumps(data, indent=2))
    except Exception as e:
        sys.stderr.write(f"hook_runner: write failed: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
