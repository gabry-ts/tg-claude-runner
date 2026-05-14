"""Pure-function parser for Claude Code JSONL transcripts.

Each line in ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl is a JSON
object describing one event in the conversation. We only care about a few:
the assistant turn (carries stop_reason and text content), tool_use blocks
(useful for live UI), and tool_result blocks. Everything else is ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


def encode_cwd(cwd: str) -> str:
    """Mirror Claude Code's projects-dir naming: non-alphanumeric → '-'."""
    return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)


def parse_line(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def stop_reason(entry: dict) -> str | None:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    return msg.get("stop_reason")


def is_assistant(entry: dict) -> bool:
    return entry.get("type") == "assistant"


def is_end_turn(entry: dict) -> bool:
    return is_assistant(entry) and stop_reason(entry) == "end_turn"


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
