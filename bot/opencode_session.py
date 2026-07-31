"""Headless OpenCode session driven via `opencode run --format json`.

Mirrors the public surface of :class:`ClaudeSession` so the bot can drive
either backend behind one interface. Each turn spawns

    opencode run --format json --auto [-s <id>] [-m <provider/model>]

with the prompt fed on stdin. In ``--format json`` mode opencode writes one
JSON object per line to stdout, each shaped ``{type, timestamp, sessionID,
...data}`` (verified against packages/opencode/src/cli/cmd/run.ts). We consume:

- ``sessionID`` (present on every event) to persist + `-s` resume,
- ``type == "text"`` events, whose ``part.text`` is a completed text block —
  concatenated in order they form the final assistant reply (there is no single
  "result" event as in Claude Code),
- ``type == "tool_use"`` events (emitted when a tool completes) for live status,
- ``type == "error"`` events to surface failures.

Conversations continue across turns/restarts via the session id saved in the
shared state.json, under a state_key the factory namespaces with "opencode:" so
it never collides with Claude Code session ids.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from . import transcript
from .claude_session import (
    ClaudeSessionError,
    RESPONSE_TIMEOUT_S,
    TextCallback,
    ToolUseCallback,
    _iter_lines,
    _kill_proc_tree,
    sessions_in_state,
)
from .session_state import clear_state, load_state, save_state

log = logging.getLogger(__name__)

OPENCODE_BIN = os.environ.get("TGCR_OPENCODE_BIN", "opencode")


class OpencodeSession:
    """A single OpenCode conversation continued via `opencode run -s <id>`.

    Thread-safety: NOT internally locked. The caller (ClaudeRunner) holds a
    per-user asyncio lock so only one ask() runs at a time.
    """

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        persist_state: bool = True,
        append_system_prompt: str | None = None,
        state_key: str = "opencode:main",
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
                "loaded persisted opencode session %s (%s)",
                self._session_id,
                self._state_key,
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
            OPENCODE_BIN,
            "run",
            "--format",
            "json",
            "--auto",  # auto-approve tool permissions (headless equivalent of
            #            Claude Code's --dangerously-skip-permissions)
        ]
        if resume_id:
            cmd += ["--session", resume_id]
        if self.model and self.model != "default":
            cmd += ["--model", self.model]
        return cmd

    def _compose_prompt(self, text: str, resume_id: str | None) -> str:
        """OpenCode's `run` has no --append-system-prompt flag, so the bot's
        conventions (proactive files, TGQUESTION marker) are injected as a
        preamble on the first turn of a new session; opencode keeps the context
        server-side, so later turns don't need to repeat it."""
        if self.append_system_prompt and not resume_id:
            return f"{self.append_system_prompt}\n\n---\n\n{text}"
        return text

    async def _run_once(
        self,
        text: str,
        resume_id: str | None,
        timeout: float,
        on_tool_use: ToolUseCallback | None,
        on_text: TextCallback | None,
    ) -> tuple[str, str | None]:
        cmd = self._build_cmd(resume_id)
        log.info(
            "spawning opencode run (resume=%s, model=%s)", bool(resume_id), self.model
        )

        env = dict(os.environ)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )

        assert proc.stdin is not None
        try:
            proc.stdin.write(self._compose_prompt(text, resume_id).encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        assert proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())

        text_parts: list[str] = []
        new_session_id: str | None = None
        error_msg: str | None = None

        async def _read_stream() -> None:
            nonlocal new_session_id, error_msg
            assert proc.stdout is not None
            async for raw in _iter_lines(proc.stdout):
                entry = transcript.parse_line(raw.decode("utf-8", "replace"))
                if entry is None:
                    continue
                etype = entry.get("type")
                sid = entry.get("sessionID")
                if sid:
                    new_session_id = sid
                if etype == "text":
                    part = entry.get("part") or {}
                    chunk = (part.get("text") or "").strip()
                    if chunk:
                        text_parts.append(chunk)
                        if on_text:
                            try:
                                await on_text(chunk)
                            except Exception as e:
                                log.debug("on_text callback failed: %s", e)
                elif etype == "tool_use":
                    if on_tool_use:
                        part = entry.get("part") or {}
                        state = part.get("state") or {}
                        tu = transcript.ToolUse(
                            id=part.get("id") or "",
                            name=part.get("tool") or "",
                            input=state.get("input") or {},
                        )
                        try:
                            await on_tool_use(tu)
                        except Exception as e:
                            log.debug("on_tool_use callback failed: %s", e)
                elif etype == "step_finish":
                    part = entry.get("part") or {}
                    cost = part.get("cost")
                    if isinstance(cost, (int, float)):
                        self.total_cost_usd += float(cost)
                    self.last_result = entry
                elif etype == "error":
                    err = entry.get("error")
                    error_msg = _error_text(err)

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
                await _read_stream()
            await proc.wait()
        finally:
            self._proc = None

        if self._cancel_requested:
            self._cancel_requested = False
            stderr_task.cancel()
            raise ClaudeSessionError("cancelled by user")

        stderr = (await stderr_task).decode("utf-8", "replace").strip()
        result_text = "\n".join(text_parts).strip()

        if proc.returncode != 0 or (error_msg and not result_text):
            detail = error_msg or stderr or f"opencode exited with code {proc.returncode}"
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
                if resume_id and self._looks_like_missing_session(err):
                    log.warning("resume %s failed (%s); retrying fresh", resume_id, e)
                    resume_id = None
                    self._session_id = None
                    continue
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
            or "session not found" in e
            or ("session" in e and "not found" in e)
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
                "429",
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
        if sessions:
            state["sessions"] = sessions
        else:
            state.pop("sessions", None)
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


def _error_text(err: object) -> str:
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        if err.get("name"):
            return str(err["name"])
        return str(err)
    return str(err) if err else "opencode error"
