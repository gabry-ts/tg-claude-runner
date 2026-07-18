"""Tests for bot/transcript.py — pure-function stream-json parsing."""

import json

from bot import transcript
from bot.transcript import (
    ToolUse,
    cost_usd,
    duration_ms,
    event_type,
    extract_text,
    extract_tool_uses,
    is_assistant,
    is_error_result,
    is_result,
    parse_line,
    result_text,
    session_id,
    usage,
)


# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------


def test_parse_line_valid_json_object():
    assert parse_line('{"type": "result", "n": 1}') == {"type": "result", "n": 1}


def test_parse_line_strips_surrounding_whitespace():
    assert parse_line('  {"a": 1}\n') == {"a": 1}


def test_parse_line_blank_returns_none():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("\n") is None
    assert parse_line("\t \r\n") is None


def test_parse_line_garbage_returns_none():
    assert parse_line("not json at all") is None
    assert parse_line("{truncated") is None
    assert parse_line('{"unterminated": "str') is None


def test_parse_line_non_object_json_still_parses():
    # parse_line does not enforce that the value is a dict.
    assert parse_line("[1, 2, 3]") == [1, 2, 3]
    assert parse_line("42") == 42
    assert parse_line('"hi"') == "hi"
    assert parse_line("null") is None  # JSON null -> Python None


def test_parse_line_roundtrip_realistic_event():
    entry = {
        "type": "assistant",
        "session_id": "sess-123",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    assert parse_line(json.dumps(entry)) == entry


# ---------------------------------------------------------------------------
# event_type / session_id
# ---------------------------------------------------------------------------


def test_event_type_present():
    assert event_type({"type": "assistant"}) == "assistant"
    assert event_type({"type": "result"}) == "result"


def test_event_type_missing_is_none():
    assert event_type({}) is None
    assert event_type({"session_id": "x"}) is None


def test_session_id_present_and_missing():
    assert session_id({"session_id": "abc-def"}) == "abc-def"
    assert session_id({}) is None
    assert session_id({"type": "result"}) is None


# ---------------------------------------------------------------------------
# is_assistant / is_result
# ---------------------------------------------------------------------------


def test_is_assistant():
    assert is_assistant({"type": "assistant"}) is True
    assert is_assistant({"type": "result"}) is False
    assert is_assistant({"type": "user"}) is False
    assert is_assistant({}) is False


def test_is_result():
    assert is_result({"type": "result"}) is True
    assert is_result({"type": "assistant"}) is False
    assert is_result({}) is False


# ---------------------------------------------------------------------------
# is_error_result
# ---------------------------------------------------------------------------


def test_is_error_result_via_is_error_flag():
    assert is_error_result({"type": "result", "is_error": True}) is True


def test_is_error_result_truthy_non_bool_flag():
    assert is_error_result({"type": "result", "is_error": 1}) is True
    assert is_error_result({"type": "result", "is_error": "yes"}) is True


def test_is_error_result_via_error_subtype():
    assert is_error_result({"type": "result", "subtype": "error"}) is True
    assert (
        is_error_result({"type": "result", "subtype": "error_during_execution"}) is True
    )
    assert is_error_result({"type": "result", "subtype": "error_max_turns"}) is True


def test_is_error_result_success_subtype_not_error():
    assert is_error_result({"type": "result", "subtype": "success"}) is False
    assert (
        is_error_result({"type": "result", "subtype": "success", "is_error": False})
        is False
    )


def test_is_error_result_plain_result_not_error():
    assert is_error_result({"type": "result"}) is False


def test_is_error_result_non_result_entries():
    # Even with error markers, non-result entries are never error results.
    assert is_error_result({"type": "assistant", "is_error": True}) is False
    assert is_error_result({"type": "system", "subtype": "error"}) is False
    assert is_error_result({}) is False


def test_is_error_result_falsy_is_error_falls_through_to_subtype():
    assert is_error_result({"type": "result", "is_error": False, "subtype": "error"}) is True
    assert (
        is_error_result({"type": "result", "is_error": None, "subtype": "success"})
        is False
    )


def test_is_error_result_non_string_subtype_coerced():
    # subtype is passed through str(); non-strings not starting with "error"
    # are not errors.
    assert is_error_result({"type": "result", "subtype": 5}) is False
    assert is_error_result({"type": "result", "subtype": None}) is False


# ---------------------------------------------------------------------------
# result_text
# ---------------------------------------------------------------------------


def test_result_text_present():
    assert result_text({"type": "result", "result": "done"}) == "done"


def test_result_text_strips_whitespace():
    assert result_text({"result": "  hi there \n"}) == "hi there"


def test_result_text_missing_or_none_is_empty():
    assert result_text({}) == ""
    assert result_text({"result": None}) == ""
    assert result_text({"result": ""}) == ""


# ---------------------------------------------------------------------------
# cost_usd
# ---------------------------------------------------------------------------


def test_cost_usd_float():
    assert cost_usd({"total_cost_usd": 0.0421}) == 0.0421


def test_cost_usd_int_coerced_to_float():
    v = cost_usd({"total_cost_usd": 3})
    assert v == 3.0
    assert isinstance(v, float)


def test_cost_usd_absent_or_wrong_type_is_none():
    assert cost_usd({}) is None
    assert cost_usd({"total_cost_usd": "0.04"}) is None
    assert cost_usd({"total_cost_usd": None}) is None
    assert cost_usd({"total_cost_usd": [0.04]}) is None


def test_cost_usd_bool_is_accepted_as_number():
    # bool is a subclass of int, so True coerces to 1.0 — documents current
    # behavior of the isinstance(v, (int, float)) check.
    assert cost_usd({"total_cost_usd": True}) == 1.0


# ---------------------------------------------------------------------------
# duration_ms
# ---------------------------------------------------------------------------


def test_duration_ms_int():
    assert duration_ms({"duration_ms": 1234}) == 1234


def test_duration_ms_float_truncated_to_int():
    v = duration_ms({"duration_ms": 1500.9})
    assert v == 1500
    assert isinstance(v, int)


def test_duration_ms_absent_or_wrong_type_is_none():
    assert duration_ms({}) is None
    assert duration_ms({"duration_ms": "1234"}) is None
    assert duration_ms({"duration_ms": None}) is None


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_usage_present():
    u = {"input_tokens": 10, "output_tokens": 20}
    assert usage({"usage": u}) == u


def test_usage_absent_or_wrong_type_is_empty_dict():
    assert usage({}) == {}
    assert usage({"usage": None}) == {}
    assert usage({"usage": "lots"}) == {}
    assert usage({"usage": [1, 2]}) == {}


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------


def _assistant(content):
    return {"type": "assistant", "message": {"content": content}}


def test_extract_text_single_block():
    assert extract_text(_assistant([{"type": "text", "text": "hello"}])) == "hello"


def test_extract_text_joins_multiple_blocks_with_newline():
    entry = _assistant(
        [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
    )
    assert extract_text(entry) == "one\ntwo"


def test_extract_text_ignores_thinking_and_tool_use():
    entry = _assistant(
        [
            {"type": "thinking", "thinking": "pondering..."},
            {"type": "text", "text": "visible"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]
    )
    assert extract_text(entry) == "visible"


def test_extract_text_non_assistant_returns_empty():
    assert extract_text({"type": "result", "result": "text"}) == ""
    assert (
        extract_text({"type": "user", "message": {"content": [{"type": "text", "text": "x"}]}})
        == ""
    )
    assert extract_text({}) == ""


def test_extract_text_missing_message_or_content():
    assert extract_text({"type": "assistant"}) == ""
    assert extract_text({"type": "assistant", "message": None}) == ""
    assert extract_text({"type": "assistant", "message": {}}) == ""
    assert extract_text({"type": "assistant", "message": {"content": None}}) == ""
    assert extract_text(_assistant([])) == ""


def test_extract_text_skips_empty_and_none_text():
    entry = _assistant(
        [
            {"type": "text", "text": ""},
            {"type": "text", "text": None},
            {"type": "text"},
            {"type": "text", "text": "kept"},
        ]
    )
    assert extract_text(entry) == "kept"


def test_extract_text_ignores_non_dict_blocks():
    entry = _assistant(["a raw string", 42, None, {"type": "text", "text": "ok"}])
    assert extract_text(entry) == "ok"


def test_extract_text_result_is_stripped():
    assert extract_text(_assistant([{"type": "text", "text": "  padded  "}])) == "padded"


def test_extract_text_only_non_text_blocks_returns_empty():
    entry = _assistant([{"type": "thinking", "thinking": "hm"}])
    assert extract_text(entry) == ""


# ---------------------------------------------------------------------------
# extract_tool_uses
# ---------------------------------------------------------------------------


def test_extract_tool_uses_full_fields():
    entry = _assistant(
        [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "Bash",
                "input": {"command": "ls -la"},
            }
        ]
    )
    uses = extract_tool_uses(entry)
    assert uses == [ToolUse(id="toolu_01", name="Bash", input={"command": "ls -la"})]


def test_extract_tool_uses_multiple_preserve_order():
    entry = _assistant(
        [
            {"type": "text", "text": "running tools"},
            {"type": "tool_use", "id": "a", "name": "Read", "input": {"file": "x"}},
            {"type": "tool_use", "id": "b", "name": "Write", "input": {"file": "y"}},
        ]
    )
    uses = extract_tool_uses(entry)
    assert [u.id for u in uses] == ["a", "b"]
    assert [u.name for u in uses] == ["Read", "Write"]


def test_extract_tool_uses_missing_keys_default():
    entry = _assistant([{"type": "tool_use"}])
    (u,) = extract_tool_uses(entry)
    assert u.id == ""
    assert u.name == ""
    assert u.input == {}


def test_extract_tool_uses_none_values_default():
    entry = _assistant(
        [{"type": "tool_use", "id": None, "name": None, "input": None}]
    )
    (u,) = extract_tool_uses(entry)
    assert u == ToolUse(id="", name="", input={})


def test_extract_tool_uses_non_assistant_returns_empty():
    entry = {
        "type": "user",
        "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]},
    }
    assert extract_tool_uses(entry) == []
    assert extract_tool_uses({}) == []


def test_extract_tool_uses_no_tool_blocks():
    assert extract_tool_uses(_assistant([{"type": "text", "text": "hi"}])) == []
    assert extract_tool_uses({"type": "assistant"}) == []
    assert extract_tool_uses({"type": "assistant", "message": None}) == []


def test_extract_tool_uses_ignores_non_dict_blocks():
    entry = _assistant(
        ["junk", None, {"type": "tool_use", "id": "z", "name": "Glob", "input": {"p": "*"}}]
    )
    uses = extract_tool_uses(entry)
    assert len(uses) == 1
    assert uses[0].name == "Glob"


def test_tool_use_dataclass_is_frozen():
    import dataclasses

    import pytest

    u = ToolUse(id="1", name="X", input={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.name = "Y"


# ---------------------------------------------------------------------------
# integration: full stream of lines
# ---------------------------------------------------------------------------


def test_full_stream_parse():
    lines = [
        "",
        '{"type": "system", "subtype": "init", "session_id": "s1"}',
        "garbage line",
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "..."},
                        {"type": "text", "text": "Working on it."},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}},
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "s1",
                "is_error": False,
                "result": "All done.\n",
                "total_cost_usd": 0.12,
                "duration_ms": 4500,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        ),
    ]
    entries = [e for e in (parse_line(l) for l in lines) if e is not None]
    assert len(entries) == 3

    assert event_type(entries[0]) == "system"
    assert not is_assistant(entries[0]) and not is_result(entries[0])

    a = entries[1]
    assert is_assistant(a)
    assert session_id(a) == "s1"
    assert extract_text(a) == "Working on it."
    assert extract_tool_uses(a) == [ToolUse(id="t1", name="Bash", input={"command": "pwd"})]

    r = entries[2]
    assert is_result(r)
    assert not is_error_result(r)
    assert result_text(r) == "All done."
    assert cost_usd(r) == 0.12
    assert duration_ms(r) == 4500
    assert usage(r) == {"input_tokens": 100, "output_tokens": 50}


def test_module_docstring_functions_exported():
    # Sanity: the module exposes exactly the helpers we test.
    for name in (
        "parse_line",
        "event_type",
        "session_id",
        "is_assistant",
        "is_result",
        "is_error_result",
        "result_text",
        "cost_usd",
        "duration_ms",
        "usage",
        "extract_text",
        "extract_tool_uses",
        "ToolUse",
    ):
        assert hasattr(transcript, name)
