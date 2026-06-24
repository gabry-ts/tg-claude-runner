"""Pure-function parser for Claude Code `stream-json` events.

`claude -p --output-format stream-json` emits one JSON object per line on
stdout. We only care about a few event types: the `assistant` turn (carries
text and tool_use content blocks), and the terminal `result` event (carries
the final consolidated text, session id, and error flag). Everything else is
ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


def parse_line(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def event_type(entry: dict) -> str | None:
    return entry.get("type")


def session_id(entry: dict) -> str | None:
    return entry.get("session_id")


def is_assistant(entry: dict) -> bool:
    return entry.get("type") == "assistant"


def is_result(entry: dict) -> bool:
    return entry.get("type") == "result"


def is_error_result(entry: dict) -> bool:
    if not is_result(entry):
        return False
    if entry.get("is_error"):
        return True
    return str(entry.get("subtype", "")).startswith("error")


def result_text(entry: dict) -> str:
    return (entry.get("result") or "").strip()


def extract_text(entry: dict) -> str:
    """Concatenate all text blocks of an assistant entry, ignoring thinking
    and tool_use. Returns "" if the entry has no text content.
    """
    if not is_assistant(entry):
        return ""
    content = (entry.get("message") or {}).get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict


def extract_tool_uses(entry: dict) -> list[ToolUse]:
    if not is_assistant(entry):
        return []
    content = (entry.get("message") or {}).get("content") or []
    out: list[ToolUse] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append(
                ToolUse(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    input=block.get("input") or {},
                )
            )
    return out
