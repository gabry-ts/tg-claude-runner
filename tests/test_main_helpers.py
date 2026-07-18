"""Tests for the pure/sync helpers in bot/main.py.

conftest.py points DATA_DIR / WORKSPACE_DIR / CLAUDE_HOME at temp dirs before
bot.main is imported. Module globals that tests touch are swapped via the
pytest ``monkeypatch`` fixture so they are restored automatically.
"""

import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.main as m
from bot.transcript import ToolUse


# ---------------------------------------------------------------------------
# is_authed
# ---------------------------------------------------------------------------

def _creds(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / ".credentials.json"
    monkeypatch.setattr(m, "CLAUDE_CREDS", path)
    return path


def test_is_authed_no_file(monkeypatch, tmp_path):
    _creds(monkeypatch, tmp_path)
    assert m.is_authed() is False


def test_is_authed_expired_without_refresh_token(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    past_ms = int((time.time() - 3600) * 1000)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": past_ms}}))
    assert m.is_authed() is False


def test_is_authed_expired_with_refresh_token(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    past_ms = int((time.time() - 3600) * 1000)
    path.write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": past_ms, "refreshToken": "rt"}})
    )
    assert m.is_authed() is True


def test_is_authed_valid_token(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    future_ms = int((time.time() + 3600) * 1000)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": future_ms}}))
    assert m.is_authed() is True


def test_is_authed_no_expires_at(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    assert m.is_authed() is True


def test_is_authed_flat_shape_without_wrapper(monkeypatch, tmp_path):
    # No claudeAiOauth wrapper: the top-level dict is used directly.
    path = _creds(monkeypatch, tmp_path)
    past_ms = int((time.time() - 3600) * 1000)
    path.write_text(json.dumps({"expiresAt": past_ms}))
    assert m.is_authed() is False


def test_is_authed_corrupt_json_assumed_usable(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    path.write_text("not-json{{{")
    assert m.is_authed() is True


# ---------------------------------------------------------------------------
# session_error_reply
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 401 from API",
        "Unauthorized request",
        "authentication_error: bad creds",
        "Invalid API key provided",
        "invalid bearer token",
        "OAuth token problem",
        "token expired",
        "your token has expired",
        "credentials revoked",
        "Please run /login first",
        "you are not logged in",
    ],
)
def test_session_error_reply_auth_markers(msg):
    out = m.session_error_reply(Exception(msg))
    assert "/login" in out
    assert "authentication failed" in out


def test_session_error_reply_generic_passthrough():
    out = m.session_error_reply(Exception("disk on fire"))
    assert out == "Claude session error: disk on fire"
    assert "/login" not in out


# ---------------------------------------------------------------------------
# save_credentials_json
# ---------------------------------------------------------------------------

def test_save_credentials_json_writes_0600(monkeypatch, tmp_path):
    path = tmp_path / "nested" / ".credentials.json"
    monkeypatch.setattr(m, "CLAUDE_CREDS", path)
    payload = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
    m.save_credentials_json(payload)
    assert path.read_text() == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_credentials_json_pops_env_override(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "CLAUDE_CREDS", tmp_path / ".credentials.json")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale")
    m.save_credentials_json("{}")
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_save_credentials_json_rejects_invalid_json(monkeypatch, tmp_path):
    path = _creds(monkeypatch, tmp_path)
    with pytest.raises(Exception):
        m.save_credentials_json("{not json")
    assert not path.exists()


# ---------------------------------------------------------------------------
# load_model / save_model
# ---------------------------------------------------------------------------

def test_model_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "MODEL_FILE", tmp_path / "models" / "model.json")
    assert m.load_model() == "default"  # no file yet
    m.save_model("opus")
    assert m.load_model() == "opus"
    m.save_model("sonnet")
    assert m.load_model() == "sonnet"


def test_load_model_corrupt_file(monkeypatch, tmp_path):
    path = tmp_path / "model.json"
    path.write_text("###garbage")
    monkeypatch.setattr(m, "MODEL_FILE", path)
    assert m.load_model() == "default"


def test_load_model_missing_key(monkeypatch, tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"other": 1}))
    monkeypatch.setattr(m, "MODEL_FILE", path)
    assert m.load_model() == "default"


# ---------------------------------------------------------------------------
# _extract_json_block
# ---------------------------------------------------------------------------

def test_extract_json_block_bare():
    assert m._extract_json_block('{"a": 1}') == {"a": 1}


def test_extract_json_block_bare_with_whitespace():
    assert m._extract_json_block('  \n {"a": 1} \n') == {"a": 1}


def test_extract_json_block_fenced():
    text = 'Here you go:\n```json\n{"files": []}\n```\nDone.'
    assert m._extract_json_block(text) == {"files": []}


def test_extract_json_block_fenced_no_lang():
    text = '```\n{"x": 2}\n```'
    assert m._extract_json_block(text) == {"x": 2}


def test_extract_json_block_embedded():
    text = 'The answer is {"k": "v"} as requested.'
    assert m._extract_json_block(text) == {"k": "v"}


def test_extract_json_block_none():
    assert m._extract_json_block("no json here at all") is None


def test_extract_json_block_invalid_braces():
    assert m._extract_json_block("some {not json} text") is None


# ---------------------------------------------------------------------------
# _resolve_workspace_path
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(monkeypatch, tmp_path):
    ws_dir = (tmp_path / "workspace").resolve()
    ws_dir.mkdir()
    monkeypatch.setattr(m, "WORKSPACE_DIR", ws_dir)
    return ws_dir


def test_resolve_workspace_path_relative(ws):
    assert m._resolve_workspace_path("sub/file.txt") == ws / "sub" / "file.txt"


def test_resolve_workspace_path_absolute_inside(ws):
    inside = ws / "a.txt"
    assert m._resolve_workspace_path(str(inside)) == inside


def test_resolve_workspace_path_traversal_rejected(ws):
    assert m._resolve_workspace_path("../escape.txt") is None
    assert m._resolve_workspace_path("sub/../../escape.txt") is None


def test_resolve_workspace_path_absolute_outside(ws):
    assert m._resolve_workspace_path("/etc/passwd") is None


def test_resolve_workspace_path_workspace_root_itself(ws):
    assert m._resolve_workspace_path(str(ws)) == ws


def test_resolve_workspace_path_dotdot_that_stays_inside(ws):
    (ws / "sub").mkdir()
    assert m._resolve_workspace_path("sub/../a.txt") == ws / "a.txt"


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------

def test_safe_filename_slashes():
    assert m._safe_filename("a/b\\c.txt") == "a_b_c.txt"


def test_safe_filename_weird_chars():
    # \w is Unicode-aware: accented letters survive, punctuation/spaces do not.
    assert m._safe_filename("héllo wörld!@#$.txt") == "héllo_wörld____.txt"
    assert m._safe_filename("a b?c*d.txt") == "a_b_c_d.txt"


def test_safe_filename_keeps_word_dot_dash():
    assert m._safe_filename("ok-name.v2_final.txt") == "ok-name.v2_final.txt"


def test_safe_filename_empty():
    assert m._safe_filename("") == "file"
    assert m._safe_filename("   ") == "file"
    assert m._safe_filename(None) == "file"


def test_safe_filename_truncates_to_200():
    out = m._safe_filename("x" * 500)
    assert out == "x" * 200
    assert len(out) == 200


# ---------------------------------------------------------------------------
# _extract_tgquestion
# ---------------------------------------------------------------------------

def _tgq(question="Pick one", options=("a", "b")):
    return "TGQUESTION: " + json.dumps({"question": question, "options": list(options)})


def test_tgquestion_valid():
    text = "Some answer.\n" + _tgq("Which?", ["red", "blue"])
    stripped, tgq = m._extract_tgquestion(text)
    assert stripped == "Some answer."
    assert tgq == {"question": "Which?", "options": ["red", "blue"]}


def test_tgquestion_missing():
    stripped, tgq = m._extract_tgquestion("plain text, no marker")
    assert stripped == "plain text, no marker"
    assert tgq is None


def test_tgquestion_one_option_ignored():
    text = "body\n" + _tgq(options=["only"])
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq is None
    assert stripped == text


def test_tgquestion_seven_options_ignored():
    text = "body\n" + _tgq(options=[f"o{i}" for i in range(7)])
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq is None
    assert stripped == text


def test_tgquestion_six_options_accepted():
    text = "body\n" + _tgq(options=[f"o{i}" for i in range(6)])
    _, tgq = m._extract_tgquestion(text)
    assert tgq is not None
    assert len(tgq["options"]) == 6


def test_tgquestion_empty_question_ignored():
    text = "body\n" + _tgq(question="  ")
    _, tgq = m._extract_tgquestion(text)
    assert tgq is None


def test_tgquestion_malformed_json():
    text = 'body\nTGQUESTION: {"question": "q", "options": [broken}'
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq is None
    assert stripped == text


def test_tgquestion_marker_not_at_line_start_ignored():
    # Marker embedded mid-line does not match the ^-anchored regex.
    text = "prefix TGQUESTION: " + json.dumps(
        {"question": "q", "options": ["a", "b"]}
    )
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq is None
    assert stripped == text


def test_tgquestion_marker_on_own_line_mid_text_still_matches():
    # Current behavior: MULTILINE regex matches a marker line anywhere in the
    # text, not only at the very end; surrounding text is glued together.
    text = "before\n" + _tgq("Mid?", ["x", "y"]) + "\nafter"
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq == {"question": "Mid?", "options": ["x", "y"]}
    assert "before" in stripped and "after" in stripped
    assert "TGQUESTION" not in stripped


def test_tgquestion_multiple_markers_last_wins():
    text = (
        "body\n"
        + _tgq("First?", ["1a", "1b"])
        + "\nmore\n"
        + _tgq("Second?", ["2a", "2b"])
    )
    stripped, tgq = m._extract_tgquestion(text)
    assert tgq["question"] == "Second?"
    # Only the last marker is removed; the first stays in the text.
    assert "First?" in stripped
    assert "Second?" not in stripped


def test_tgquestion_options_truncated_to_60_chars():
    long_opt = "z" * 100
    text = "body\n" + _tgq(options=[long_opt, "short"])
    _, tgq = m._extract_tgquestion(text)
    assert tgq["options"][0] == "z" * 60
    assert tgq["options"][1] == "short"


def test_tgquestion_blank_options_filtered_out():
    # Two real options plus blanks: blanks are dropped, still valid.
    text = "body\n" + _tgq(options=["a", "  ", "b", ""])
    _, tgq = m._extract_tgquestion(text)
    assert tgq["options"] == ["a", "b"]


# ---------------------------------------------------------------------------
# _mentioned_files
# ---------------------------------------------------------------------------

def test_mentioned_files_found_in_order(ws):
    a = ws / "a.txt"
    b = ws / "b.txt"
    a.write_text("A")
    b.write_text("B")
    text = f"See {b} and also {a} for details."
    assert m._mentioned_files(text) == [b, a]


def test_mentioned_files_dedupe(ws):
    a = ws / "a.txt"
    a.write_text("A")
    text = f"{a} mentioned twice: {a}"
    assert m._mentioned_files(text) == [a]


def test_mentioned_files_cap_six(ws):
    paths = []
    for i in range(8):
        p = ws / f"f{i}.txt"
        p.write_text(str(i))
        paths.append(p)
    text = " ".join(str(p) for p in paths)
    found = m._mentioned_files(text)
    assert found == paths[:6]


def test_mentioned_files_nonexistent_skipped(ws):
    a = ws / "real.txt"
    a.write_text("x")
    text = f"{ws}/ghost.txt and {a}"
    assert m._mentioned_files(text) == [a]


def test_mentioned_files_directory_skipped(ws):
    d = ws / "dir"
    d.mkdir()
    assert m._mentioned_files(f"look in {d}") == []


def test_mentioned_files_traversal_rejected(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    text = f"read {ws}/../secret.txt now"
    assert m._mentioned_files(text) == []


def test_mentioned_files_trailing_punctuation_stripped(ws):
    a = ws / "a.txt"
    a.write_text("A")
    for punct in [",", ".", ";", ":", ".,"]:
        assert m._mentioned_files(f"open {a}{punct} thanks") == [a]


def test_mentioned_files_none_mentioned(ws):
    assert m._mentioned_files("no paths here") == []


# ---------------------------------------------------------------------------
# _speakable
# ---------------------------------------------------------------------------

def test_speakable_code_blocks_replaced():
    out = m._speakable("before\n```python\nprint('x')\n```\nafter")
    assert "(codice)" in out
    assert "print" not in out


def test_speakable_inline_code_unwrapped():
    assert m._speakable("run `ls -la` now") == "run ls -la now"


def test_speakable_links_become_label():
    assert m._speakable("see [the docs](https://example.com) here") == "see the docs here"


def test_speakable_markdown_chars_stripped():
    assert m._speakable("*bold* _ital_ #head ~strike~") == "bold ital head strike"


def test_speakable_blank_line_collapse():
    assert m._speakable("a\n\n\nb\n\nc") == "a\nb\nc"


def test_speakable_combined():
    text = "# Title\n\nUse `cmd`.\n\n```\nblock\n```\n\n[link](http://x)"
    out = m._speakable(text)
    assert out == "Title\nUse cmd.\n (codice) \nlink" or "(codice)" in out
    assert "```" not in out
    assert "http://x" not in out


# ---------------------------------------------------------------------------
# _cb_store
# ---------------------------------------------------------------------------

def test_cb_store_roundtrip_and_uniqueness(monkeypatch):
    monkeypatch.setattr(m, "_CB_CACHE", {})
    tokens = [m._cb_store(f"payload-{i}") for i in range(50)]
    assert len(set(tokens)) == 50
    for i, tok in enumerate(tokens):
        assert m._CB_CACHE[tok] == f"payload-{i}"
        assert len(tok) == 12


def test_cb_store_bounded(monkeypatch):
    monkeypatch.setattr(m, "_CB_CACHE", {})
    max_seen = 0
    for i in range(800):
        m._cb_store(i)
        max_seen = max(max_seen, len(m._CB_CACHE))
    assert max_seen <= 301
    # Eviction drops the oldest entries, newest survive.
    assert 800 - 1 in list(m._CB_CACHE.values())


def test_cb_store_eviction_removes_oldest(monkeypatch):
    monkeypatch.setattr(m, "_CB_CACHE", {})
    first = m._cb_store("oldest")
    for i in range(400):
        m._cb_store(i)
    assert first not in m._CB_CACHE


# ---------------------------------------------------------------------------
# _remember_sent
# ---------------------------------------------------------------------------

def test_remember_sent_stores(monkeypatch):
    monkeypatch.setattr(m, "_SENT_CACHE", {})
    m._remember_sent(1, "hello")
    assert m._SENT_CACHE[1] == "hello"


def test_remember_sent_cap_30_evicts_oldest_10(monkeypatch):
    monkeypatch.setattr(m, "_SENT_CACHE", {})
    for i in range(30):
        m._remember_sent(i, f"msg{i}")
    assert len(m._SENT_CACHE) == 30
    m._remember_sent(30, "msg30")
    assert len(m._SENT_CACHE) == 21
    for i in range(10):
        assert i not in m._SENT_CACHE  # oldest 10 evicted
    for i in range(10, 31):
        assert m._SENT_CACHE[i] == f"msg{i}"


def test_remember_sent_never_exceeds_30(monkeypatch):
    monkeypatch.setattr(m, "_SENT_CACHE", {})
    for i in range(120):
        m._remember_sent(i, "x")
        assert len(m._SENT_CACHE) <= 30


# ---------------------------------------------------------------------------
# _fmt_secs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secs,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (59.9, "59s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (7325, "2h 2m"),
        (86400, "24h 0m"),
    ],
)
def test_fmt_secs(secs, expected):
    assert m._fmt_secs(secs) == expected


# ---------------------------------------------------------------------------
# _format_tool
# ---------------------------------------------------------------------------

def _tu(name, inp=None):
    return ToolUse(id="t1", name=name, input=inp or {})


def test_format_tool_bash_first_line_truncated():
    cmd = "echo " + "a" * 100 + "\nsecond line"
    label = m._format_tool(_tu("Bash", {"command": cmd}))
    assert label.startswith("🔧 Bash: echo ")
    assert "second" not in label
    assert len(label.split(": ", 1)[1]) == 60


def test_format_tool_read_write_edit_basename():
    assert m._format_tool(_tu("Read", {"file_path": "/a/b/c.py"})) == "📖 Read c.py"
    assert m._format_tool(_tu("Write", {"file_path": "/a/x.md"})) == "✏️ Write x.md"
    assert m._format_tool(_tu("Edit", {"file_path": "/a/y.md"})) == "✏️ Edit y.md"


def test_format_tool_read_missing_path():
    assert m._format_tool(_tu("Read")) == "📖 Read ?"


def test_format_tool_glob_grep():
    assert m._format_tool(_tu("Glob", {"pattern": "**/*.py"})) == "🔍 Glob **/*.py"
    assert m._format_tool(_tu("Grep", {"pattern": "def main"})) == "🔍 Grep def main"
    long_pat = "x" * 80
    assert m._format_tool(_tu("Grep", {"pattern": long_pat})) == "🔍 Grep " + "x" * 50


def test_format_tool_task():
    assert m._format_tool(_tu("Task", {"subagent_type": "explorer"})) == "🤖 Subagent: explorer"
    assert m._format_tool(_tu("Task")) == "🤖 Subagent: task"


def test_format_tool_web():
    assert m._format_tool(_tu("WebFetch", {"url": "https://example.com/page"})) == (
        "🌐 Fetch https://example.com/page"
    )
    assert m._format_tool(_tu("WebFetch")) == "🌐 Fetch "
    assert m._format_tool(_tu("WebSearch", {"query": "weather"})) == "🔍 Search: weather"


def test_format_tool_todowrite():
    assert m._format_tool(_tu("TodoWrite")) == "📝 Todos updated"


def test_format_tool_unknown_fallback():
    assert m._format_tool(_tu("SomethingNew")) == "🔧 SomethingNew"


# ---------------------------------------------------------------------------
# allowed
# ---------------------------------------------------------------------------

def _fake_update(user_id):
    user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(effective_user=user)


def test_allowed_matching_user():
    assert m.allowed(_fake_update(m.ALLOWED_USER)) is True


def test_allowed_foreign_user():
    assert m.allowed(_fake_update(m.ALLOWED_USER + 1)) is False


def test_allowed_no_user():
    assert m.allowed(_fake_update(None)) is False
