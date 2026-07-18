"""Tests for bot/claude_session.py.

Drives ClaudeSession against fake `claude` executables: small bash scripts
written to a tmp dir that read the prompt from stdin, then emit stream-json
lines (or fail in controlled ways). bot.claude_session.CLAUDE_BIN is
monkeypatched to point at the fake for each test.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bot import claude_session as cs
from bot import session_state, transcript


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

BASH_HEADER = "#!/bin/bash\n"


def make_fake_claude(tmp_path: Path, body: str, name: str = "fake-claude") -> Path:
    """Write an executable bash script that plays the role of `claude`."""
    script = tmp_path / name
    script.write_text(BASH_HEADER + body)
    script.chmod(0o755)
    return script


def make_session(tmp_path: Path, **kwargs) -> cs.ClaudeSession:
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    kwargs.setdefault("persist_state", False)
    return cs.ClaudeSession(cwd=cwd, **kwargs)


HAPPY_BODY = """\
cat >/dev/null
cat <<'EOF'
{"type":"system","subtype":"init","session_id":"sess-123"}
{"type":"assistant","message":{"content":[{"type":"text","text":"Hello there"}]},"session_id":"sess-123"}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu-1","name":"Bash","input":{"command":"ls"}}]},"session_id":"sess-123"}
{"type":"result","subtype":"success","is_error":false,"result":"final answer","session_id":"sess-123","total_cost_usd":0.05,"duration_ms":123}
EOF
"""


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_result_and_captures_session_id(tmp_path, monkeypatch):
    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    result = asyncio.run(session.ask("hi"))

    assert result == "final answer"
    assert session.session_id == "sess-123"


def test_prompt_is_fed_on_stdin(tmp_path, monkeypatch):
    body = """\
prompt=$(cat)
printf '{"type":"result","subtype":"success","result":"echo:%s","session_id":"sid-echo"}\\n' "$prompt"
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    result = asyncio.run(session.ask("hello world"))

    assert result == "echo:hello world"


def test_callbacks_fire_in_order_with_correct_payloads(tmp_path, monkeypatch):
    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    events: list[tuple[str, object]] = []

    async def on_text(chunk: str) -> None:
        events.append(("text", chunk))

    async def on_tool_use(tu: transcript.ToolUse) -> None:
        events.append(("tool", tu))

    result = asyncio.run(session.ask("hi", on_text=on_text, on_tool_use=on_tool_use))

    assert result == "final answer"
    assert [kind for kind, _ in events] == ["text", "tool"]
    assert events[0][1] == "Hello there"
    tu = events[1][1]
    assert isinstance(tu, transcript.ToolUse)
    assert tu.id == "tu-1"
    assert tu.name == "Bash"
    assert tu.input == {"command": "ls"}


def test_callback_exceptions_are_swallowed(tmp_path, monkeypatch):
    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    async def bad_text(chunk: str) -> None:
        raise RuntimeError("text callback boom")

    async def bad_tool(tu: transcript.ToolUse) -> None:
        raise RuntimeError("tool callback boom")

    result = asyncio.run(session.ask("hi", on_text=bad_text, on_tool_use=bad_tool))

    assert result == "final answer"


def test_last_result_meta_and_cost_accumulates_across_asks(tmp_path, monkeypatch):
    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    assert session.last_result is None
    assert session.total_cost_usd == 0.0

    asyncio.run(session.ask("first"))
    assert session.last_result is not None
    assert session.last_result["type"] == "result"
    assert session.last_result["total_cost_usd"] == 0.05
    assert session.last_result["duration_ms"] == 123
    assert session.total_cost_usd == pytest.approx(0.05)

    asyncio.run(session.ask("second"))
    assert session.total_cost_usd == pytest.approx(0.10)


def test_falls_back_to_accumulated_text_without_result_event(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
cat <<'EOF'
{"type":"assistant","message":{"content":[{"type":"text","text":"partial output"}]},"session_id":"sess-acc"}
EOF
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    result = asyncio.run(session.ask("hi"))

    assert result == "partial output"
    assert session.session_id == "sess-acc"


# ---------------------------------------------------------------------------
# --resume retry on missing session
# ---------------------------------------------------------------------------

def test_resume_retry_when_session_missing(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
for a in "$@"; do
  if [ "$a" = "--resume" ]; then
    echo "No conversation found with session ID stale-id" >&2
    exit 1
  fi
done
echo '{"type":"result","subtype":"success","result":"fresh start","session_id":"new-sess"}'
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)
    session._session_id = "stale-id"

    result = asyncio.run(session.ask("hi"))

    assert result == "fresh start"
    assert session.session_id == "new-sess"


def test_missing_session_error_without_resume_is_not_retried(tmp_path, monkeypatch):
    # Same error text but no stale id: nothing to drop, so it surfaces.
    body = """\
cat >/dev/null
echo "No conversation found with session ID whatever" >&2
exit 1
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    with pytest.raises(cs.ClaudeSessionError, match="No conversation found"):
        asyncio.run(session.ask("hi"))


# ---------------------------------------------------------------------------
# transient retry with backoff
# ---------------------------------------------------------------------------

def _transient_script(tmp_path: Path, fail_times: int) -> Path:
    counter = tmp_path / "counter"
    body = f"""\
cat >/dev/null
count=$(cat {counter} 2>/dev/null || echo 0)
count=$((count+1))
echo $count > {counter}
if [ "$count" -le {fail_times} ]; then
  echo "API Error: 529 overloaded" >&2
  exit 1
fi
echo '{{"type":"result","subtype":"success","result":"third time lucky","session_id":"sess-529"}}'
"""
    return make_fake_claude(tmp_path, body)


def _capture_sleeps(monkeypatch) -> list[float]:
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return delays


def test_transient_error_retried_twice_with_2s_then_4s_backoff(tmp_path, monkeypatch):
    script = _transient_script(tmp_path, fail_times=2)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    delays = _capture_sleeps(monkeypatch)
    session = make_session(tmp_path)

    result = asyncio.run(session.ask("hi"))

    assert result == "third time lucky"
    assert session.session_id == "sess-529"
    # Exponential backoff: 2s after the first failure, 4s after the second.
    assert delays == [2, 4]
    assert (tmp_path / "counter").read_text().strip() == "3"


def test_transient_error_gives_up_after_two_retries(tmp_path, monkeypatch):
    script = _transient_script(tmp_path, fail_times=99)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    delays = _capture_sleeps(monkeypatch)
    session = make_session(tmp_path)

    with pytest.raises(cs.ClaudeSessionError, match="529 overloaded"):
        asyncio.run(session.ask("hi"))

    assert delays == [2, 4]
    # 1 initial attempt + 2 retries = 3 spawns.
    assert (tmp_path / "counter").read_text().strip() == "3"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_cancel_current_kills_promptly_even_with_child_holding_stdout(tmp_path, monkeypatch):
    # The `sleep 60` child inherits the stdout pipe; killing only the parent
    # would leave the read blocked. _kill_proc_tree kills the whole group.
    body = """\
cat >/dev/null
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]},"session_id":"sess-c"}'
sleep 60 &
wait
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    async def run() -> float:
        task = asyncio.create_task(session.ask("hi"))
        for _ in range(200):
            if session._proc is not None:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("claude subprocess never started")
        # Give the script time to fork the sleep child.
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        assert session.cancel_current() is True
        with pytest.raises(cs.ClaudeSessionError, match="cancelled by user"):
            await task
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"cancel took {elapsed:.1f}s; child kept the pipe open?"


def test_cancel_current_with_nothing_running_returns_false(tmp_path):
    session = make_session(tmp_path)
    assert session.cancel_current() is False


def test_cancel_current_after_run_finished_returns_false(tmp_path, monkeypatch):
    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)
    asyncio.run(session.ask("hi"))
    assert session.cancel_current() is False


# ---------------------------------------------------------------------------
# timeout / errors
# ---------------------------------------------------------------------------

def test_timeout_raises_no_response_within(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
sleep 30
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    t0 = time.monotonic()
    with pytest.raises(cs.ClaudeSessionError, match="no response within 1s"):
        asyncio.run(session.ask("hi", timeout=1.0))
    assert time.monotonic() - t0 < 10.0


def test_nonzero_exit_raises_with_stderr_detail(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
echo "kaboom: something exploded" >&2
exit 3
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    with pytest.raises(cs.ClaudeSessionError, match="kaboom: something exploded"):
        asyncio.run(session.ask("hi"))


def test_nonzero_exit_without_stderr_reports_exit_code(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
exit 7
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    with pytest.raises(cs.ClaudeSessionError, match="exited with code 7"):
        asyncio.run(session.ask("hi"))


def test_error_result_with_no_text_raises(tmp_path, monkeypatch):
    body = """\
cat >/dev/null
echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"","session_id":"sess-err"}'
exit 0
"""
    script = make_fake_claude(tmp_path, body)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))
    session = make_session(tmp_path)

    with pytest.raises(cs.ClaudeSessionError, match="exited with code 0"):
        asyncio.run(session.ask("hi"))


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------

BASE_FLAGS = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--dangerously-skip-permissions",
]


def test_build_cmd_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path)
    assert session._build_cmd(None) == ["fake-claude"] + BASE_FLAGS


def test_build_cmd_with_resume_id(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path)
    assert session._build_cmd("abc-123") == (
        ["fake-claude"] + BASE_FLAGS + ["--resume", "abc-123"]
    )


def test_build_cmd_model_default_omits_model_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path, model="default")
    assert session._build_cmd(None) == ["fake-claude"] + BASE_FLAGS


def test_build_cmd_with_explicit_model(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path, model="opus")
    assert session._build_cmd(None) == (
        ["fake-claude"] + BASE_FLAGS + ["--model", "opus"]
    )


def test_build_cmd_with_append_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path, append_system_prompt="be brief")
    assert session._build_cmd(None) == (
        ["fake-claude"] + BASE_FLAGS + ["--append-system-prompt", "be brief"]
    )


def test_build_cmd_all_options_ordered(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CLAUDE_BIN", "fake-claude")
    session = make_session(tmp_path, model="sonnet", append_system_prompt="asp")
    assert session._build_cmd("rid") == (
        ["fake-claude"]
        + BASE_FLAGS
        + ["--resume", "rid", "--model", "sonnet", "--append-system-prompt", "asp"]
    )


# ---------------------------------------------------------------------------
# error-classification marker tables
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "err,expected",
    [
        ("No conversation found with session ID abc", True),
        ("no conversation found", True),
        ("There is no session to resume", True),
        ("Session abc-123 not found", True),
        ("could not resume: transcript not found", True),
        ("", False),
        ("overloaded", False),
        ("cancelled by user", False),
        ("some random failure", False),
        ("kaboom: something exploded", False),
    ],
)
def test_looks_like_missing_session(err, expected):
    assert cs.ClaudeSession._looks_like_missing_session(err) is expected


@pytest.mark.parametrize(
    "err,expected",
    [
        ("API Error: 529 overloaded", True),
        ("Overloaded", True),
        ("529", True),
        ("502 Bad Gateway", True),
        ("503 Service Unavailable", True),
        ("504 Gateway Timeout", True),
        ("Internal Server Error", True),
        ("ECONNRESET", True),
        ("ETIMEDOUT", True),
        ("ECONNREFUSED", True),
        ("fetch failed", True),
        ("network error", True),
        ("connection error", True),
        ("socket hang up", True),
        # NOT transient: cancels and timeouts must never be retried, even if
        # a transient marker also appears in the text.
        ("cancelled by user", False),
        ("no response within 60s", False),
        ("cancelled by user after 529 overloaded", False),
        ("", False),
        ("syntax error", False),
        ("No conversation found", False),
    ],
)
def test_looks_transient(err, expected):
    assert cs.ClaudeSession._looks_transient(err) is expected


# ---------------------------------------------------------------------------
# persistence / reset / set_model
# ---------------------------------------------------------------------------

def test_persist_and_reset_clears_session_id_and_state(tmp_path, monkeypatch):
    state_file = tmp_path / "state" / "state.json"
    monkeypatch.setattr(session_state, "STATE_DIR", state_file.parent)
    monkeypatch.setattr(session_state, "STATE_FILE", state_file)

    script = make_fake_claude(tmp_path, HAPPY_BODY)
    monkeypatch.setattr(cs, "CLAUDE_BIN", str(script))

    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)

    session = cs.ClaudeSession(cwd=cwd, persist_state=True)
    assert session.session_id is None
    asyncio.run(session.ask("hi"))
    assert session.session_id == "sess-123"
    assert state_file.exists()

    # A new session with matching cwd + model picks up the persisted id.
    resumed = cs.ClaudeSession(cwd=cwd, persist_state=True)
    assert resumed.session_id == "sess-123"

    # Mismatched model must not load the stale id.
    other_model = cs.ClaudeSession(cwd=cwd, model="opus", persist_state=True)
    assert other_model.session_id is None

    # Mismatched cwd must not load the stale id either.
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    assert cs.ClaudeSession(cwd=other_cwd, persist_state=True).session_id is None

    asyncio.run(resumed.reset())
    assert resumed.session_id is None
    assert not state_file.exists()


def test_reset_without_persistence_clears_in_memory_id(tmp_path):
    session = make_session(tmp_path)
    session._session_id = "in-memory"
    asyncio.run(session.reset())
    assert session.session_id is None


def test_kill_is_reset(tmp_path):
    session = make_session(tmp_path)
    session._session_id = "in-memory"
    asyncio.run(session.kill())
    assert session.session_id is None


def test_set_model(tmp_path):
    session = make_session(tmp_path, model="opus")
    assert session.set_model("opus") is False
    assert session.set_model("sonnet") is True
    assert session.model == "sonnet"
    assert session.set_model(None) is True
    assert session.model is None
