"""Backend selection: drive either Claude Code or OpenCode behind one interface.

``TGCR_BACKEND`` (env, default "claude") sets the default; a runtime override is
persisted in state.json under "backend", so it survives restarts and can be
switched from Telegram with /backend. The two backends never share session ids:
the factory namespaces opencode state keys with an "opencode:" prefix, so an
existing Claude state file keeps working untouched.
"""

from __future__ import annotations

from pathlib import Path

from .session_state import load_state, save_state

VALID = ("claude", "opencode")

# Read the env default lazily so tests can set it before import side effects.
import os

_DEFAULT = os.environ.get("TGCR_BACKEND", "claude").strip().lower()
if _DEFAULT not in VALID:
    _DEFAULT = "claude"


def current() -> str:
    b = load_state().get("backend")
    return b if b in VALID else _DEFAULT


def set_current(name: str) -> bool:
    name = name.strip().lower()
    if name not in VALID:
        return False
    state = load_state()
    state["backend"] = name
    save_state(state)
    return True


def is_opencode() -> bool:
    return current() == "opencode"


def make_session(
    *,
    cwd: Path,
    model: str | None = None,
    persist_state: bool = True,
    append_system_prompt: str | None = None,
    state_key: str = "main",
):
    """Construct the session object for the active backend. Both classes share
    the same public surface (ask/reset/kill/set_model/cancel_current/...)."""
    if is_opencode():
        from .opencode_session import OpencodeSession

        return OpencodeSession(
            cwd=cwd,
            model=model,
            persist_state=persist_state,
            append_system_prompt=append_system_prompt,
            state_key=f"opencode:{state_key}",
        )
    from .claude_session import ClaudeSession

    return ClaudeSession(
        cwd=cwd,
        model=model,
        persist_state=persist_state,
        append_system_prompt=append_system_prompt,
        state_key=state_key,
    )
