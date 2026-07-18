"""Regression: stream-json lines bigger than asyncio's 64 KiB readline limit
(e.g. base64 image content after a photo upload) must not kill the turn."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

import bot.claude_session as cs
from bot.claude_session import ClaudeSession


@pytest.fixture
def big_line_claude(tmp_path, monkeypatch):
    # one assistant event with ~5 MB of text on a single line, then the result
    big = "x" * (5 * 1024 * 1024)
    payload = tmp_path / "events.jsonl"
    with payload.open("w") as fh:
        fh.write(json.dumps({
            "type": "assistant", "session_id": "s-big",
            "message": {"content": [{"type": "text", "text": big}]},
        }) + "\n")
        fh.write(json.dumps({
            "type": "result", "session_id": "s-big",
            "result": "done after big line", "is_error": False,
        }) + "\n")
    script = tmp_path / "fake_claude"
    script.write_text(f"#!/bin/bash\ncat > /dev/null\ncat {payload}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    return script


def test_huge_stream_line_does_not_crash(big_line_claude, tmp_path):
    s = ClaudeSession(cwd=tmp_path, persist_state=False)
    chunks = []

    async def on_text(c):
        chunks.append(len(c))

    result = asyncio.run(s.ask("hi", timeout=30, on_text=on_text))
    assert result == "done after big line"
    assert chunks == [5 * 1024 * 1024]
    assert s.session_id == "s-big"


def test_iter_lines_splits_and_handles_missing_trailing_newline():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"a\nbb\nccc")  # last line has no newline
        reader.feed_eof()
        return [l async for l in cs._iter_lines(reader)]

    assert asyncio.run(run()) == [b"a", b"bb", b"ccc"]
