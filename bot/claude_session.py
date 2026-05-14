"""Interactive Claude Code session driven via libtmux + JSONL tail.

Claude Code runs inside a persistent tmux window. We inject prompts with
send-keys and read the structured transcript JSONL that Claude writes to
~/.claude/projects/<encoded-cwd>/<session_id>.jsonl.

Why this exists: Anthropic stops counting `claude -p` against subscription
plans on 2026-06-15. Interactive use still counts against the plan, so we
drive Claude interactively instead of as a one-shot subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiofiles
import libtmux
from libtmux.exc import LibTmuxException

from . import transcript

ToolUseCallback = Callable[[transcript.ToolUse], Awaitable[None]]
from .session_state import (
    SESSION_MAP_FILE,
    clear_session_map,
    load_state,
    save_state,
)

log = logging.getLogger(__name__)

TMUX_SESSION_NAME = os.environ.get("TGCR_TMUX_SESSION", "tgcr")
TMUX_WINDOW_NAME = os.environ.get("TGCR_TMUX_WINDOW", "claude")
CLAUDE_BIN = os.environ.get("TGCR_CLAUDE_BIN", "claude")

SESSION_ID_TIMEOUT_S = float(os.environ.get("TGCR_SESSION_ID_TIMEOUT", "30"))
RESPONSE_TIMEOUT_S = float(os.environ.get("TGCR_RESPONSE_TIMEOUT", "300"))
JSONL_POLL_S = float(os.environ.get("TGCR_JSONL_POLL", "1.0"))
SEND_KEYS_PAUSE_S = float(os.environ.get("TGCR_SEND_PAUSE", "0.5"))
BOOT_GRACE_S = float(os.environ.get("TGCR_BOOT_GRACE", "3.0"))
PROMPT_CHECK_EVERY_N = int(os.environ.get("TGCR_PROMPT_CHECK_EVERY", "5"))
END_TURN_GRACE_S = float(os.environ.get("TGCR_END_TURN_GRACE", "10.0"))

_PERMISSION_PROMPT_PATTERNS = [
    re.compile(r"^\s*Do you want to (proceed|make|create|delete|allow)", re.M),
    re.compile(r"^\s*❯\s*1\.\s*Yes", re.M),
    re.compile(r"^\s*Bash command", re.M),
    re.compile(r"^\s*This command requires approval", re.M),
]

_ONBOARDING_PATTERNS = [
    re.compile(r"Choose the text style", re.M),
    re.compile(r"Let's get started", re.M),
    re.compile(r"Use Claude's default IDE", re.M),
    re.compile(r"Tell us about your role", re.M),
    re.compile(r"Press Enter to continue", re.M),
]

_BYPASS_WARNING_PATTERN = re.compile(
    r"Bypass Permissions mode.*accept all responsibility", re.S
)


def _looks_like_permission_prompt(pane_text: str) -> bool:
    return any(p.search(pane_text) for p in _PERMISSION_PROMPT_PATTERNS)


def _looks_like_onboarding(pane_text: str) -> bool:
    return any(p.search(pane_text) for p in _ONBOARDING_PATTERNS)


def _looks_like_bypass_warning(pane_text: str) -> bool:
    return _BYPASS_WARNING_PATTERN.search(pane_text) is not None


@dataclass
class SessionInfo:
    session_id: str
    cwd: str
    window_id: str
    jsonl_path: Path
    jsonl_offset: int = 0
    model: str | None = None
    pending_tools: dict[str, str] = field(default_factory=dict)


class ClaudeSessionError(RuntimeError):
    pass


class ClaudeSession:
    """One persistent Claude Code window inside a tmux session.

    Thread-safety: NOT internally locked. The caller (ClaudeRunner) holds a
    per-user asyncio lock so only one ask() runs at a time.
    """

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        tmux_window: str = TMUX_WINDOW_NAME,
        persist_state: bool = True,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self._tmux_window = tmux_window
        self._persist_state = persist_state
        self._server: libtmux.Server | None = None
        self._info: SessionInfo | None = None

    @property
    def info(self) -> SessionInfo | None:
        return self._info

    def _claude_cmd(self, resume_id: str | None) -> str:
        parts = ["env", "IS_SANDBOX=1", CLAUDE_BIN, "--dangerously-skip-permissions"]
        if resume_id:
            parts += ["--resume", resume_id]
        if self.model and self.model != "default":
            parts += ["--model", self.model]
        return " ".join(parts)

    def _ensure_server(self) -> libtmux.Server:
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def _get_or_create_tmux_session(self) -> libtmux.Session:
        server = self._ensure_server()
        try:
            sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
            return sess
        except Exception:
            pass
        return server.new_session(
            session_name=TMUX_SESSION_NAME,
            start_directory=str(self.cwd),
            kill_session=False,
            attach=False,
            x=200,
            y=50,
        )

    def _find_window(self, sess: libtmux.Session) -> libtmux.Window | None:
        for w in sess.windows:
            if w.window_name == self._tmux_window:
                return w
        return None

    async def _spawn_window(self, resume_id: str | None) -> libtmux.Window:
        def _do() -> libtmux.Window:
            sess = self._get_or_create_tmux_session()
            existing = self._find_window(sess)
            if existing is not None:
                existing.kill()
            window = sess.new_window(
                window_name=self._tmux_window,
                start_directory=str(self.cwd),
                attach=False,
                window_shell=self._claude_cmd(resume_id),
            )
            return window

        return await asyncio.to_thread(_do)

    async def _wait_for_session_id(
        self, timeout: float, window: "libtmux.Window | None" = None
    ) -> tuple[str, str]:
        deadline = time.time() + timeout
        last_dismiss = 0.0
        while time.time() < deadline:
            if SESSION_MAP_FILE.exists():
                try:
                    import json
                    data = json.loads(SESSION_MAP_FILE.read_text())
                    sid = data.get("session_id")
                    cwd = data.get("cwd")
                    if sid and cwd:
                        return sid, cwd
                except Exception:
                    pass

            now = time.time()
            if window is not None and now - last_dismiss > 3.0:
                last_dismiss = now
                await self._try_dismiss_onboarding(window)
            await asyncio.sleep(0.3)

        # Final diagnostic: log what claude is showing so we know what we're stuck on
        if window is not None:
            def _final_capture() -> str:
                try:
                    pane = window.active_pane
                    if pane is None:
                        return ""
                    return "\n".join(pane.capture_pane())
                except Exception:
                    return ""
            pane_text = await asyncio.to_thread(_final_capture)
            if pane_text:
                log.error("timeout pane content (last 30 lines):\n%s",
                          "\n".join(pane_text.splitlines()[-30:]))

        raise ClaudeSessionError(
            f"SessionStart hook did not write session_map within {timeout}s"
        )

    async def _try_dismiss_onboarding(self, window: "libtmux.Window") -> None:
        def _capture() -> str:
            try:
                pane = window.active_pane
                if pane is None:
                    return ""
                return "\n".join(pane.capture_pane())
            except Exception:
                return ""

        pane_text = await asyncio.to_thread(_capture)
        if not pane_text:
            return

        if _looks_like_bypass_warning(pane_text):
            # Default selection is "1. No, exit" — we must pick "2. Yes, I accept"
            log.warning("dismissing bypass-permissions warning: selecting Yes (2)+Enter")

            def _do() -> None:
                try:
                    pane = window.active_pane
                    if pane is None:
                        return
                    pane.send_keys("2", enter=False, literal=True)
                except Exception as e:
                    log.debug("bypass select 2 failed: %s", e)

            await asyncio.to_thread(_do)
            await asyncio.sleep(0.3)

            def _enter() -> None:
                try:
                    pane = window.active_pane
                    if pane is not None:
                        pane.send_keys("", enter=True, literal=False)
                except Exception as e:
                    log.debug("bypass enter failed: %s", e)

            await asyncio.to_thread(_enter)
            return

        if _looks_like_onboarding(pane_text):
            log.warning("dismissing onboarding screen (Enter accepts default)")

            def _send() -> None:
                try:
                    pane = window.active_pane
                    if pane is not None:
                        pane.send_keys("", enter=True, literal=False)
                except Exception as e:
                    log.debug("onboarding dismiss send_keys failed: %s", e)

            await asyncio.to_thread(_send)

    def _jsonl_path(self, session_id: str, cwd: str) -> Path:
        return (
            Path.home()
            / ".claude"
            / "projects"
            / transcript.encode_cwd(cwd)
            / f"{session_id}.jsonl"
        )

    async def ensure_started(self) -> SessionInfo:
        if self._info is not None and await self._is_window_alive():
            return self._info

        resume_id: str | None = None
        if self._persist_state:
            saved = load_state()
            if (
                saved.get("session_id")
                and saved.get("cwd") == str(self.cwd)
                and saved.get("model") == self.model
            ):
                candidate = self._jsonl_path(saved["session_id"], saved["cwd"])
                if candidate.exists():
                    resume_id = saved["session_id"]
                    log.info("resuming claude session %s", resume_id)

        clear_session_map()
        window = await self._spawn_window(resume_id)
        log.info(
            "spawned tmux window %s (resume=%s, persist=%s)",
            window.window_id, bool(resume_id), self._persist_state,
        )

        session_id, hook_cwd = await self._wait_for_session_id(
            SESSION_ID_TIMEOUT_S, window=window
        )
        jsonl = self._jsonl_path(session_id, hook_cwd)
        offset = jsonl.stat().st_size if jsonl.exists() else 0

        self._info = SessionInfo(
            session_id=session_id,
            cwd=hook_cwd,
            window_id=window.window_id or "",
            jsonl_path=jsonl,
            jsonl_offset=offset,
            model=self.model,
        )
        if self._persist_state:
            save_state(
                {
                    "session_id": session_id,
                    "cwd": str(self.cwd),
                    "model": self.model,
                    "window_id": self._info.window_id,
                }
            )
        log.info(
            "claude session ready id=%s jsonl=%s offset=%d",
            session_id, jsonl, offset,
        )
        await asyncio.sleep(BOOT_GRACE_S)
        return self._info

    async def _is_window_alive(self) -> bool:
        if self._info is None:
            return False

        def _check() -> bool:
            try:
                server = self._ensure_server()
                sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
                for w in sess.windows:
                    if w.window_id == self._info.window_id:
                        return True
            except Exception:
                return False
            return False

        return await asyncio.to_thread(_check)

    async def send_prompt(self, text: str) -> None:
        if self._info is None:
            raise ClaudeSessionError("session not started")

        def _send() -> None:
            server = self._ensure_server()
            sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
            pane = None
            for w in sess.windows:
                if w.window_id == self._info.window_id:
                    pane = w.active_pane
                    break
            if pane is None:
                raise ClaudeSessionError("active pane not found")
            pane.send_keys(text, enter=False, literal=True)

        await asyncio.to_thread(_send)
        await asyncio.sleep(SEND_KEYS_PAUSE_S)

        def _enter() -> None:
            server = self._ensure_server()
            sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
            for w in sess.windows:
                if w.window_id == self._info.window_id:
                    pane = w.active_pane
                    if pane is None:
                        raise ClaudeSessionError("active pane not found")
                    pane.send_keys("", enter=True, literal=False)
                    return
            raise ClaudeSessionError("window not found")

        await asyncio.to_thread(_enter)

    async def _capture_pane(self) -> str:
        if self._info is None:
            return ""

        def _do() -> str:
            try:
                server = self._ensure_server()
                sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
                for w in sess.windows:
                    if w.window_id == self._info.window_id:
                        pane = w.active_pane
                        if pane is None:
                            return ""
                        return "\n".join(pane.capture_pane())
            except Exception as e:
                log.debug("capture_pane failed: %s", e)
            return ""

        return await asyncio.to_thread(_do)

    async def _auto_accept_permission(self) -> None:
        log.warning("permission prompt detected in pane; auto-answering 1+Enter")

        def _do() -> None:
            server = self._ensure_server()
            sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
            for w in sess.windows:
                if w.window_id == self._info.window_id:
                    pane = w.active_pane
                    if pane is not None:
                        pane.send_keys("1", enter=False, literal=True)
                        return

        await asyncio.to_thread(_do)
        await asyncio.sleep(0.2)

        def _enter() -> None:
            server = self._ensure_server()
            sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
            for w in sess.windows:
                if w.window_id == self._info.window_id:
                    pane = w.active_pane
                    if pane is not None:
                        pane.send_keys("", enter=True, literal=False)
                        return

        await asyncio.to_thread(_enter)

    async def wait_for_response(
        self,
        timeout: float = RESPONSE_TIMEOUT_S,
        on_tool_use: ToolUseCallback | None = None,
    ) -> str:
        """Tail the JSONL until the assistant turn is complete.

        Claude Code 2.1.x with extended thinking sometimes splits one logical
        turn into TWO assistant entries, both flagged stop_reason=end_turn —
        first one carrying only a `thinking` block, the second carrying the
        actual `text` a few seconds later. Returning on the first one would
        yield an empty response and shift every subsequent reply by one turn.

        Strategy: on every end_turn without text, start a grace timer. Keep
        reading; if a later entry with text arrives, return it. If the grace
        expires with no text, then return empty (a genuine no-response turn).
        """
        if self._info is None:
            raise ClaudeSessionError("session not started")
        path = self._info.jsonl_path
        deadline = time.time() + timeout

        last_text = ""
        empty_end_turn_at = 0.0
        iter_count = 0
        while time.time() < deadline:
            if not path.exists():
                await asyncio.sleep(JSONL_POLL_S)
                continue
            try:
                async with aiofiles.open(path, "r") as f:
                    await f.seek(self._info.jsonl_offset)
                    while True:
                        pre_read = await f.tell()
                        line = await f.readline()
                        if not line:
                            break
                        if not line.strip():
                            self._info.jsonl_offset = await f.tell()
                            continue
                        entry = transcript.parse_line(line)
                        if entry is None:
                            # likely a partial line still being written —
                            # rewind so we re-read it whole next poll
                            await f.seek(pre_read)
                            break
                        self._info.jsonl_offset = await f.tell()
                        if transcript.is_assistant(entry):
                            text = transcript.extract_text(entry)
                            if text:
                                last_text = text
                            if on_tool_use:
                                for tu in transcript.extract_tool_uses(entry):
                                    try:
                                        await on_tool_use(tu)
                                    except Exception as e:
                                        log.debug("on_tool_use callback failed: %s", e)
                            if transcript.is_end_turn(entry):
                                if last_text:
                                    return last_text
                                if empty_end_turn_at == 0.0:
                                    empty_end_turn_at = time.time()
                                    log.debug(
                                        "end_turn observed without text; waiting "
                                        "%.1fs for a follow-up entry",
                                        END_TURN_GRACE_S,
                                    )
            except FileNotFoundError:
                await asyncio.sleep(JSONL_POLL_S)
                continue

            if (
                empty_end_turn_at > 0.0
                and not last_text
                and time.time() - empty_end_turn_at >= END_TURN_GRACE_S
            ):
                log.warning(
                    "end_turn had no text after %.1fs grace; returning empty",
                    END_TURN_GRACE_S,
                )
                return ""

            if empty_end_turn_at > 0.0 and last_text:
                return last_text

            iter_count += 1
            if iter_count % PROMPT_CHECK_EVERY_N == 0:
                pane = await self._capture_pane()
                if pane and _looks_like_permission_prompt(pane):
                    await self._auto_accept_permission()
            await asyncio.sleep(JSONL_POLL_S)

        raise ClaudeSessionError(
            f"no end_turn within {timeout}s; last text fragment len={len(last_text)}"
        )

    async def ask(
        self,
        text: str,
        timeout: float = RESPONSE_TIMEOUT_S,
        on_tool_use: ToolUseCallback | None = None,
    ) -> str:
        await self.ensure_started()
        await self.send_prompt(text)
        return await self.wait_for_response(timeout, on_tool_use=on_tool_use)

    async def reset(self) -> None:
        def _kill() -> None:
            if self._info is None:
                return
            try:
                server = self._ensure_server()
                sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
                for w in sess.windows:
                    if w.window_id == self._info.window_id:
                        w.kill()
                        break
            except (LibTmuxException, Exception) as e:
                log.warning("reset kill failed: %s", e)

        await asyncio.to_thread(_kill)
        self._info = None
        clear_session_map()
        if self._persist_state:
            save_state({})

    async def kill(self) -> None:
        await self.reset()
        def _kill_server() -> None:
            try:
                server = self._ensure_server()
                sess = server.sessions.get(session_name=TMUX_SESSION_NAME)
                sess.kill()
            except Exception:
                pass

        await asyncio.to_thread(_kill_server)
        self._server = None

    def set_model(self, model: str | None) -> bool:
        if model == self.model:
            return False
        self.model = model
        return True
