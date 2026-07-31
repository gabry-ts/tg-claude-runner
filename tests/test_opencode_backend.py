"""Unit tests for the OpenCode backend: selection, generic auth onboarding,
and the session command/prompt/parse wiring. These run without Telegram."""

import asyncio
import json
from pathlib import Path

import pytest

from bot import backend, opencode_auth
from bot.opencode_session import OpencodeSession
from bot.session_state import load_state, save_state


@pytest.fixture(autouse=True)
def _reset_backend():
    yield
    # Never leave the persisted backend flipped for the next test.
    st = load_state()
    st.pop("backend", None)
    save_state(st)


# ---- backend selection ----------------------------------------------------

def test_default_backend_is_claude():
    assert backend.current() == "claude"
    assert backend.is_opencode() is False


def test_set_and_persist_backend():
    assert backend.set_current("opencode") is True
    assert backend.current() == "opencode"
    assert backend.is_opencode() is True
    assert load_state().get("backend") == "opencode"


def test_invalid_backend_rejected():
    assert backend.set_current("gpt4") is False
    assert backend.current() == "claude"


def test_make_session_dispatches_and_namespaces(tmp_path):
    backend.set_current("opencode")
    sess = backend.make_session(cwd=tmp_path, state_key="work", persist_state=False)
    assert isinstance(sess, OpencodeSession)
    # OpenCode state keys are namespaced so they never collide with Claude's.
    assert sess._state_key == "opencode:work"


# ---- generic provider onboarding -----------------------------------------

def test_save_and_list_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    opencode_auth.save_api_key("anthropic", "sk-abc123")
    opencode_auth.save_api_key("openrouter", "or-xyz")
    assert opencode_auth.providers() == ["anthropic", "openrouter"]
    stored = json.loads((tmp_path / "opencode" / "auth.json").read_text())
    assert stored["anthropic"] == {"type": "api", "key": "sk-abc123"}
    assert opencode_auth.is_authed() is True


def test_save_api_key_normalizes_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    opencode_auth.save_api_key("OpenAI/", "  key-1  ")
    assert opencode_auth.providers() == ["openai"]
    assert json.loads((tmp_path / "opencode" / "auth.json").read_text())["openai"]["key"] == "key-1"
    with pytest.raises(ValueError):
        opencode_auth.save_api_key("bad id!", "k")
    with pytest.raises(ValueError):
        opencode_auth.save_api_key("openai", "   ")


def test_is_authed_false_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for k in list(__import__("os").environ):
        if k.endswith("_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    assert opencode_auth.is_authed() is False


# ---- session command / prompt / error wiring -----------------------------

def test_build_cmd_new_session(tmp_path):
    s = OpencodeSession(cwd=tmp_path, persist_state=False)
    cmd = s._build_cmd(None)
    assert cmd[:5] == ["opencode", "run", "--format", "json", "--auto"]
    assert "--session" not in cmd


def test_build_cmd_resume_and_model(tmp_path):
    s = OpencodeSession(cwd=tmp_path, model="anthropic/claude-sonnet-4-6", persist_state=False)
    cmd = s._build_cmd("ses_123")
    assert "--session" in cmd and cmd[cmd.index("--session") + 1] == "ses_123"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-6"


def test_default_model_omitted(tmp_path):
    s = OpencodeSession(cwd=tmp_path, model="default", persist_state=False)
    assert "--model" not in s._build_cmd(None)


def test_system_preamble_only_on_new_session(tmp_path):
    s = OpencodeSession(cwd=tmp_path, append_system_prompt="RULES", persist_state=False)
    assert s._compose_prompt("ciao", None).startswith("RULES")
    assert s._compose_prompt("ciao", "ses_1") == "ciao"  # resumed turn: no repeat


def test_missing_session_detection():
    assert OpencodeSession._looks_like_missing_session("Error: session not found") is True
    assert OpencodeSession._looks_like_missing_session("overloaded") is False


def test_persist_and_reset_roundtrip(tmp_path):
    s = OpencodeSession(cwd=tmp_path, state_key="opencode:main")
    s._session_id = "ses_999"
    s._save_session_id()
    assert load_state()["sessions"]["opencode:main"]["session_id"] == "ses_999"
    asyncio.run(s.reset())
    assert "opencode:main" not in load_state().get("sessions", {})
