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
from pathlib import Path
from typing import Awaitable, Callable

from . import transcript

ToolUseCallback = Callable[[transcript.ToolUse], Awaitable[None]]

from .session_state import clear_state, load_state, save_state

log = logging.getLogger(__name__)

CLAUDE_BIN = os.environ.get("TGCR_CLAUDE_BIN", "claude")
RESPONSE_TIMEOUT_S = float(os.environ.get("TGCR_RESPONSE_TIMEOUT", "1800"))


class ClaudeSessionError(RuntimeError):
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
    ) -> None:
        self.cwd = cwd
        self.model = model
        self._persist_state = persist_state
        self._session_id: str | None = None
        if persist_state:
            self._load_session_id()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _load_session_id(self) -> None:
        saved = load_state()
        if (
            saved.get("session_id")
            and saved.get("cwd") == str(self.cwd)
            and saved.get("model") == self.model
        ):
            self._session_id = saved["session_id"]
            log.info("loaded persisted session %s", self._session_id)

    def _save_session_id(self) -> None:
        if not self._persist_state:
            return
        save_state(
            {
                "session_id": self._session_id,
                "cwd": str(self.cwd),
                "model": self.model,
            }
        )

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
        return cmd

    async def _run_once(
        self,
        text: str,
        resume_id: str | None,
        timeout: float,
        on_tool_use: ToolUseCallback | None,
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
                    if on_tool_use:
                        for tu in transcript.extract_tool_uses(entry):
                            try:
                                await on_tool_use(tu)
                            except Exception as e:
                                log.debug("on_tool_use callback failed: %s", e)
                elif transcript.is_result(entry):
                    final_text = transcript.result_text(entry)
                    is_error = transcript.is_error_result(entry)

        try:
            await asyncio.wait_for(_read_stream(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stderr_task.cancel()
            raise ClaudeSessionError(f"no response within {timeout:.0f}s")

        await proc.wait()
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
    ) -> str:
        resume_id = self._session_id
        try:
            result, sid = await self._run_once(text, resume_id, timeout, on_tool_use)
        except ClaudeSessionError as e:
            # A stale or missing session id (e.g. after the workspace was wiped)
            # makes --resume fail. Drop it and retry once from a fresh session.
            if resume_id and self._looks_like_missing_session(str(e)):
                log.warning("resume %s failed (%s); retrying fresh", resume_id, e)
                self._session_id = None
                result, sid = await self._run_once(text, None, timeout, on_tool_use)
            else:
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

    async def reset(self) -> None:
        self._session_id = None
        if self._persist_state:
            clear_state()

    async def kill(self) -> None:
        await self.reset()

    def set_model(self, model: str | None) -> bool:
        if model == self.model:
            return False
        self.model = model
        return True
