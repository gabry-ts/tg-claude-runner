"""Headless Claude Code session driven via `claude -p` (print mode).

Each turn spawns `claude -p --output-format stream-json` as a subprocess. The
prompt is fed on stdin; the structured JSONL event stream is read back from
stdout, giving us the assistant text, live tool_use blocks, and the session
id. Conversations are continued across turns with `--resume <session_id>`,
which also lets a session survive bot restarts via the id saved in state.json.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Awaitable, Callable

from . import transcript

ToolUseCallback = Callable[[transcript.ToolUse], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]

from .session_state import clear_state, load_state, save_state

log = logging.getLogger(__name__)


def sessions_in_state(state: dict) -> dict:
    """Named-session dict from persisted state, migrating the legacy
    single-session flat shape ({session_id, cwd, model}) to {'main': {...}}."""
    sessions = state.get("sessions")
    if isinstance(sessions, dict):
        return sessions
    if state.get("session_id"):
        return {
            "main": {k: state.get(k) for k in ("session_id", "cwd", "model")}
        }
    return {}

CLAUDE_BIN = os.environ.get("TGCR_CLAUDE_BIN", "claude")
# 0 (default) = no timeout: a turn runs until claude finishes or /cancel kills it.
RESPONSE_TIMEOUT_S = float(os.environ.get("TGCR_RESPONSE_TIMEOUT", "0"))


class ClaudeSessionError(RuntimeError):
    pass


def _kill_proc_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group (claude + any children it spawned).

    Children inherit the stdout pipe: killing only claude would leave them
    holding it open and the stream read blocked until they exit on their own.
    Requires the process to have been started with start_new_session=True.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ClaudeSession:
    """A single Claude Code conversation continued via `claude -p --resume`.

    Thread-safety: NOT internally locked. The caller (ClaudeRunner) holds a
    per-user asyncio lock so only one ask() runs at a time.
    """

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        persist_state: bool = True,
        append_system_prompt: str | None = None,
        state_key: str = "main",
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.append_system_prompt = append_system_prompt
        self._persist_state = persist_state
        self._state_key = state_key
        self._session_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self.last_result: dict | None = None
        self.total_cost_usd = 0.0
        if persist_state:
            self._load_session_id()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _load_session_id(self) -> None:
        entry = sessions_in_state(load_state()).get(self._state_key) or {}
        if (
            entry.get("session_id")
            and entry.get("cwd") == str(self.cwd)
            and entry.get("model") == self.model
        ):
            self._session_id = entry["session_id"]
            log.info(
                "loaded persisted session %s (%s)", self._session_id, self._state_key
            )

    def _save_session_id(self) -> None:
        if not self._persist_state:
            return
        state = load_state()
        sessions = sessions_in_state(state)
        sessions[self._state_key] = {
            "session_id": self._session_id,
            "cwd": str(self.cwd),
            "model": self.model,
        }
        for legacy in ("session_id", "cwd", "model"):
            state.pop(legacy, None)
        state["sessions"] = sessions
        save_state(state)

    def _build_cmd(self, resume_id: str | None) -> list[str]:
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if resume_id:
            cmd += ["--resume", resume_id]
        if self.model and self.model != "default":
            cmd += ["--model", self.model]
        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]
        return cmd

    async def _run_once(
        self,
        text: str,
        resume_id: str | None,
        timeout: float,
        on_tool_use: ToolUseCallback | None,
        on_text: TextCallback | None,
    ) -> tuple[str, str | None]:
        cmd = self._build_cmd(resume_id)
        log.info("spawning claude -p (resume=%s, model=%s)", bool(resume_id), self.model)

        env = dict(os.environ)
        env["IS_SANDBOX"] = "1"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        # Feed the prompt on stdin, then close it so claude starts working.
        assert proc.stdin is not None
        try:
            proc.stdin.write(text.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        # Drain stderr concurrently so a chatty process can never deadlock on a
        # full stderr pipe while we read stdout.
        assert proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())

        accumulated: list[str] = []
        final_text = ""
        new_session_id: str | None = None
        is_error = False

        async def _read_stream() -> None:
            nonlocal final_text, new_session_id, is_error
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                entry = transcript.parse_line(raw.decode("utf-8", "replace"))
                if entry is None:
                    continue
                sid = transcript.session_id(entry)
                if sid:
                    new_session_id = sid
                if transcript.is_assistant(entry):
                    chunk = transcript.extract_text(entry)
                    if chunk:
                        accumulated.append(chunk)
                        if on_text:
                            try:
                                await on_text(chunk)
                            except Exception as e:
                                log.debug("on_text callback failed: %s", e)
                    if on_tool_use:
                        for tu in transcript.extract_tool_uses(entry):
                            try:
                                await on_tool_use(tu)
                            except Exception as e:
                                log.debug("on_tool_use callback failed: %s", e)
                elif transcript.is_result(entry):
                    final_text = transcript.result_text(entry)
                    is_error = transcript.is_error_result(entry)
                    self.last_result = entry
                    cost = transcript.cost_usd(entry)
                    if cost:
                        self.total_cost_usd += cost

        self._proc = proc
        try:
            if timeout > 0:
                try:
                    await asyncio.wait_for(_read_stream(), timeout=timeout)
                except asyncio.TimeoutError:
                    _kill_proc_tree(proc)
                    await proc.wait()
                    stderr_task.cancel()
                    raise ClaudeSessionError(f"no response within {timeout:.0f}s")
            else:
                # No timeout: read until claude exits (or cancel_current()
                # kills the process, which closes stdout and ends the read).
                await _read_stream()
            await proc.wait()
        finally:
            self._proc = None

        if self._cancel_requested:
            self._cancel_requested = False
            stderr_task.cancel()
            raise ClaudeSessionError("cancelled by user")

        stderr = (await stderr_task).decode("utf-8", "replace").strip()

        result_text = final_text or "\n".join(accumulated).strip()

        if proc.returncode != 0 or (is_error and not result_text):
            detail = stderr or result_text or f"claude exited with code {proc.returncode}"
            raise ClaudeSessionError(detail[:500])

        return result_text, new_session_id

    async def ask(
        self,
        text: str,
        timeout: float = RESPONSE_TIMEOUT_S,
        on_tool_use: ToolUseCallback | None = None,
        on_text: TextCallback | None = None,
    ) -> str:
        resume_id = self._session_id
        transient_tries = 0
        while True:
            try:
                result, sid = await self._run_once(
                    text, resume_id, timeout, on_tool_use, on_text
                )
                break
            except ClaudeSessionError as e:
                err = str(e)
                # A stale or missing session id (e.g. after the workspace was
                # wiped) makes --resume fail. Drop it and retry fresh.
                if resume_id and self._looks_like_missing_session(err):
                    log.warning("resume %s failed (%s); retrying fresh", resume_id, e)
                    resume_id = None
                    self._session_id = None
                    continue
                # Transient API/network errors (overloaded, 5xx, connection
                # drops) are retried with backoff instead of surfacing.
                if self._looks_transient(err) and transient_tries < 2:
                    transient_tries += 1
                    delay = 2 ** transient_tries
                    log.warning(
                        "transient error (%s); retry %d/2 in %ds",
                        e, transient_tries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        if sid:
            self._session_id = sid
            self._save_session_id()
        return result

    @staticmethod
    def _looks_like_missing_session(err: str) -> bool:
        e = err.lower()
        return (
            "no conversation found" in e
            or "no session" in e
            or ("session" in e and "not found" in e)
            or ("resume" in e and "found" in e)
        )

    @staticmethod
    def _looks_transient(err: str) -> bool:
        e = err.lower()
        if "cancelled by user" in e or "no response within" in e:
            return False
        return any(
            m in e
            for m in (
                "overloaded",
                "529",
                "502",
                "503",
                "504",
                "internal server error",
                "econnreset",
                "etimedout",
                "econnrefused",
                "fetch failed",
                "network error",
                "connection error",
                "socket hang up",
            )
        )

    def cancel_current(self) -> bool:
        """Kill the in-flight `claude -p` run, if any. Safe to call from a
        handler that does NOT hold the runner lock: the ask() awaiting the
        killed process sees the cancel flag and raises 'cancelled by user'.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        self._cancel_requested = True
        _kill_proc_tree(proc)
        return True

    async def reset(self) -> None:
        self._session_id = None
        if not self._persist_state:
            return
        state = load_state()
        sessions = sessions_in_state(state)
        sessions.pop(self._state_key, None)
        for legacy in ("session_id", "cwd", "model"):
            state.pop(legacy, None)
        state.pop("sessions", None)
        if sessions:
            state["sessions"] = sessions
        if state:
            save_state(state)
        else:
            clear_state()

    async def kill(self) -> None:
        await self.reset()

    def set_model(self, model: str | None) -> bool:
        if model == self.model:
            return False
        self.model = model
        return True
