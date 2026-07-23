#!/usr/bin/env python3
"""Telegram bot wrapping the Claude Code CLI."""

import os
import re
import json
import time
import uuid
import base64
import asyncio
import logging
import tempfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque

from telegram import (
    Update,
    BotCommand,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    MessageReactionHandler,
    PollAnswerHandler,
    filters,
    ContextTypes,
)

from .markdown_v2 import to_telegram_markdown, split_for_telegram
from .scheduler import Scheduler
from .claude_session import (
    ClaudeSession,
    ClaudeSessionError,
    ToolUseCallback,
    sessions_in_state,
)
from .session_state import load_state, save_state
from . import oauth
from . import transcript
from .transcript import ToolUse

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tg-claude-runner")


class _RingHandler(logging.Handler):
    """Keeps the last N log lines in memory so /logs can serve them."""

    def __init__(self, capacity: int = 400) -> None:
        super().__init__()
        self.buf: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


_ring = _RingHandler()
_ring.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_ring)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ["TELEGRAM_ALLOWED_USER"])
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", "/home/node/.claude"))
CLAUDE_CREDS = CLAUDE_HOME / ".credentials.json"
JOBS_FILE = DATA_DIR / "jobs.json"
MODEL_FILE = DATA_DIR / "model.json"
CHAT_FILE = DATA_DIR / "chat.json"
QUICK_FILE = DATA_DIR / "quick.json"
PIN_FILE = DATA_DIR / "pinned.json"
# Telegram message-effect id for 🎉 (private chats only); used on replies to
# runs longer than LONG_RUN_S together with a big 👍 reaction.
EFFECT_TADA_ID = "5046509860389126442"
LONG_RUN_S = 300
# (label shown on the Telegram button, value passed to `claude --model`).
# Aliases track the latest version; full IDs pin one. Free text via
# `/model <name>` is also accepted for anything not listed.
MODEL_CHOICES = [
    ("default", "default"),
    ("Opus", "opus"),
    ("Sonnet", "sonnet"),
    ("Haiku", "haiku"),
    ("Opus Plan", "opusplan"),
    ("Sonnet 5", "claude-sonnet-5"),
    ("Opus 4.8", "claude-opus-4-8"),
    ("Haiku 4.5", "claude-haiku-4-5-20251001"),
]
MODEL_VALUES = [v for _, v in MODEL_CHOICES]
_MODEL_NAME_RE = re.compile(r"^[\w.\-\[\]]{1,64}$")
_SESSION_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
TTS_ENABLED = bool(OPENAI_API_KEY) and os.environ.get("TGCR_TTS", "1") != "0"
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
SAVED_DIR_NAME = "saved"

if OPENAI_API_KEY:
    from openai import OpenAI
    _openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    _openai_client = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
INBOX_DIR_NAME = "inbox"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram bot API limit

_FILE_SEARCH_SYSTEM = (
    "You are a file retrieval assistant for a Telegram bot. "
    "Given the user's natural-language request, find file(s) under /workspace/ that match. "
    "Use Read, Glob, and Bash tools to search. Do NOT create, modify, or delete files. "
    "Respond with a single JSON object only — no commentary, no code fence. "
    "Schema: "
    '{"files":[{"title":"<short human title>","path":"<absolute path under /workspace>"}],'
    '"note":"<optional brief comment>"}. '
    'If nothing matches, return {"files":[], "note":"<reason>"}.'
)


# Teaches Claude to emit multiple-choice questions in a machine-readable
# marker the bot renders as Telegram inline buttons (see _extract_tgquestion).
_TGQ_SYSTEM_PROMPT = (
    "You are talking to your user through a Telegram bot. When you need the "
    "user to pick between concrete options, end your reply with one extra "
    "line in exactly this format (valid JSON, last line, nothing after it):\n"
    'TGQUESTION: {"question": "<short question>", "options": ["<opt 1>", "<opt 2>"]}\n'
    "Use 2-6 short options (max 60 chars each), only when a choice is "
    'genuinely needed. Add "multi": true when the user may pick several '
    "options (rendered as a Telegram poll). Open-ended questions stay "
    "normal text.\n"
    "Formatting: put long log/output dumps in markdown blockquote lines "
    "(> ...) — they render as a collapsed, expandable quote. Short runnable "
    "commands go in fenced code blocks (they get one-tap copy buttons)."
)

# Gives Claude a way to take initiative: anything dropped into the notify
# inbox is delivered to the user's Telegram chat by the bot (see check_notify).
_PROACTIVE_PROMPT = (
    "Proactive messages: to send the user a Telegram message outside the "
    "current conversation (from a background process, a cron job, or a script "
    f"you set up), write a small text/markdown file into {WORKSPACE_DIR}/.notify/ "
    "— the bot delivers its content to the user within seconds and deletes "
    "the file. For an actionable notification, write JSON instead: "
    '{"text": "<optional message>", "question": "<question>", '
    '"options": ["<opt>", ...], "multi": false} — the user answers via '
    "buttons (or a poll if multi) and the answer reaches you as a new turn."
)

_SYSTEM_APPENDIX = _TGQ_SYSTEM_PROMPT + "\n\n" + _PROACTIVE_PROMPT


class ClaudeRunner:
    """Named persistent Claude Code conversations driven via `claude -p`.

    Single-user bot: any number of named sessions (default "main") with one
    current at a time, plus ephemeral sessions for isolated lookups (see
    ask_isolated()). Lock per uid is kept for API compatibility even though
    we currently only allow ALLOWED_USER.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sessions: dict[str, ClaudeSession] = {}
        self._busy_since: float | None = None
        current = load_state().get("current")
        self._current = (
            current
            if isinstance(current, str) and _SESSION_NAME_RE.match(current)
            else "main"
        )

    @property
    def current_name(self) -> str:
        return self._current

    def _wanted_model(self) -> str | None:
        m = load_model()
        return None if m == "default" else m

    def _get_session(self, name: str) -> ClaudeSession:
        sess = self._sessions.get(name)
        if sess is None:
            sess = ClaudeSession(
                cwd=WORKSPACE_DIR,
                model=self._wanted_model(),
                persist_state=True,
                append_system_prompt=_SYSTEM_APPENDIX,
                state_key=name,
            )
            self._sessions[name] = sess
        return sess

    def session_names(self) -> list[str]:
        names = set(self._sessions) | {self._current, "main"}
        names |= set(sessions_in_state(load_state()))
        return sorted(names)

    def _persist_current(self) -> None:
        state = load_state()
        state["current"] = self._current
        save_state(state)

    async def switch(self, uid: int, name: str) -> None:
        async with self._locks[uid]:
            self._current = name
            self._persist_current()

    async def delete(self, uid: int, name: str) -> bool:
        async with self._locks[uid]:
            sess = self._sessions.pop(name, None)
            state = load_state()
            # A just-switched-to session may have no persisted turns yet but
            # still "exists" from the user's point of view.
            existed = (
                sess is not None
                or name in sessions_in_state(state)
                or name == self._current
            )
            if sess is not None:
                await sess.reset()  # clears its persisted entry too
            else:
                sessions = sessions_in_state(state)
                if name in sessions:
                    sessions.pop(name)
                    for legacy in ("session_id", "cwd", "model"):
                        state.pop(legacy, None)
                    state["sessions"] = sessions
                    save_state(state)
            if self._current == name:
                self._current = "main"
                self._persist_current()
            return existed

    def busy(self, uid: int) -> bool:
        return self._locks[uid].locked()

    def cancel(self, uid: int) -> bool:
        """Kill the in-flight run of the current session, if any.

        Deliberately does NOT take the lock — the whole point is to interrupt
        the ask() currently holding it. Queued asks behind it still run.
        """
        sess = self._sessions.get(self._current)
        if sess is None:
            return False
        return sess.cancel_current()

    async def reset(self, uid: int, name: str | None = None) -> None:
        async with self._locks[uid]:
            sess = self._sessions.get(name or self._current)
            if sess is not None:
                await sess.reset()

    async def ask(
        self,
        uid: int,
        text: str,
        on_tool_use: ToolUseCallback | None = None,
        on_text=None,
        session: str | None = None,
    ) -> str:
        """Run a turn on `session` (default: the current one) — forum topics
        pass their own session name so topics never touch the global current."""
        async with self._locks[uid]:
            self._busy_since = time.time()
            try:
                name = session or self._current
                wanted = self._wanted_model()
                sess = self._get_session(name)
                if sess.model != wanted:
                    log.info(
                        "model switched %s -> %s; resetting session %s",
                        sess.model, wanted, name,
                    )
                    await sess.reset()
                    sess.set_model(wanted)

                log.info("ask[%s]: %s", name, text[:200])
                return await sess.ask(
                    text, on_tool_use=on_tool_use, on_text=on_text
                )
            finally:
                self._busy_since = None

    def status_info(self) -> dict:
        s = self._sessions.get(self._current)
        return {
            "session_name": self._current,
            "session_id": s.session_id if s else None,
            "busy_since": self._busy_since,
            "last_result": s.last_result if s else None,
            "total_cost_usd": s.total_cost_usd if s else 0.0,
        }

    async def ask_isolated(self, prompt: str, model: str | None = None) -> str:
        """Run an isolated one-shot query in a fresh, non-persisted session.

        Used by /file and scheduled jobs so their context never pollutes the
        chat sessions: runs without --resume and never saves a session id.
        `model` overrides the globally selected model (scheduled-job routing).
        """
        wanted = model if model and model != "default" else self._wanted_model()
        sess = ClaudeSession(
            cwd=WORKSPACE_DIR,
            model=wanted,
            persist_state=False,
            append_system_prompt=_PROACTIVE_PROMPT,
        )
        try:
            return await sess.ask(prompt)
        finally:
            try:
                await sess.kill()
            except Exception as e:
                log.warning("isolated session cleanup failed: %s", e)


claude = ClaudeRunner()
scheduler: Scheduler | None = None


def is_authed() -> bool:
    """True if credentials exist and are usable.

    ``expiresAt`` only bounds the short-lived access token: when a
    ``refreshToken`` is present the CLI silently renews the access token on
    its next invocation and rewrites the credentials file, so refusing to run
    Claude here would block that refresh and force a pointless /login every
    few hours. Only credentials that are expired AND lack a refresh token are
    truly dead (Claude would 401 on every message) — catching that case lets
    the bot ask for /login instead of failing on every message.
    """
    if not CLAUDE_CREDS.exists():
        return False
    try:
        data = json.loads(CLAUDE_CREDS.read_text())
        oauth = data.get("claudeAiOauth") or data
        expires_at = oauth.get("expiresAt")
        refresh_token = oauth.get("refreshToken")
    except Exception:
        return True  # unknown shape — assume usable, let Claude surface errors
    if refresh_token or not expires_at:
        return True
    return expires_at > time.time() * 1000


_AUTH_ERROR_MARKERS = (
    "401",
    "unauthor",
    "authentication_error",
    "invalid api key",
    "invalid bearer token",
    "oauth token",
    "token expired",
    "token has expired",
    "revoked",
    "please run /login",
    "not logged in",
)


def session_error_reply(e: Exception) -> str:
    """Telegram-friendly message for a failed Claude run.

    Auth failures get an explicit /login hint instead of a raw CLI error —
    otherwise the user has no way to tell a dead token from a transient error.
    """
    err = str(e).lower()
    if any(m in err for m in _AUTH_ERROR_MARKERS):
        return (
            "⚠️ Claude authentication failed — credentials expired or revoked. "
            "Use /login to re-authenticate."
        )
    return f"Claude session error: {e}"


def save_credentials_json(creds_json: str) -> None:
    json.loads(creds_json)
    CLAUDE_CREDS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_CREDS.write_text(creds_json)
    CLAUDE_CREDS.chmod(0o600)
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


def _load_chat_id() -> int | None:
    try:
        return int(json.loads(CHAT_FILE.read_text())["chat_id"])
    except Exception:
        return None


def _save_chat_id(chat_id: int) -> None:
    try:
        if _load_chat_id() == chat_id:
            return
        CHAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_FILE.write_text(json.dumps({"chat_id": chat_id}))
    except Exception as e:
        log.warning("could not persist chat id: %s", e)


def load_model() -> str:
    try:
        return json.loads(MODEL_FILE.read_text()).get("model", "default")
    except Exception:
        return "default"


def save_model(name: str) -> None:
    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_FILE.write_text(json.dumps({"model": name}))


def allowed(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ALLOWED_USER


def _extract_json_block(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _resolve_workspace_path(raw: str) -> Path | None:
    """Return an absolute path under WORKSPACE_DIR, or None if outside / invalid."""
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE_DIR / p
    try:
        resolved = p.resolve()
    except Exception:
        return None
    ws = WORKSPACE_DIR.resolve()
    try:
        resolved.relative_to(ws)
    except ValueError:
        return None
    return resolved


async def _send_file_to(bot, chat_id: int, path: Path, caption: str | None = None) -> None:
    if not path.exists() or not path.is_file():
        await bot.send_message(chat_id=chat_id, text=f"Not found: {path}")
        return
    ext = path.suffix.lower()
    try:
        with path.open("rb") as fh:
            if ext in IMAGE_EXTS:
                await bot.send_photo(chat_id=chat_id, photo=fh, caption=caption)
            else:
                await bot.send_document(
                    chat_id=chat_id, document=fh, filename=path.name, caption=caption
                )
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Failed to send {path.name}: {e}")


async def _send_file(update: Update, path: Path, caption: str | None = None) -> None:
    await _send_file_to(update.get_bot(), update.effective_chat.id, path, caption)


# In-memory store for inline-button payloads: Telegram caps callback_data at
# 64 bytes, so buttons carry a short token that maps to the real payload here.
# Lost on restart — stale buttons answer "expired".
_CB_CACHE: dict[str, object] = {}


def _cb_store(payload: object) -> str:
    if len(_CB_CACHE) > 300:
        for k in list(_CB_CACHE)[:100]:
            _CB_CACHE.pop(k, None)
    token = uuid.uuid4().hex[:12]
    _CB_CACHE[token] = payload
    return token


_TGQ_RE = re.compile(r"^TGQUESTION:\s*(\{.*\})\s*$", re.MULTILINE)


def _extract_tgquestion(text: str) -> tuple[str, dict | None]:
    """Split off a trailing TGQUESTION marker (see _TGQ_SYSTEM_PROMPT)."""
    m = None
    for m in _TGQ_RE.finditer(text):
        pass  # last occurrence wins
    if m is None:
        return text, None
    try:
        parsed = json.loads(m.group(1))
        question = str(parsed.get("question") or "").strip()
        options = [
            str(o).strip()[:60] for o in (parsed.get("options") or []) if str(o).strip()
        ]
    except Exception:
        return text, None
    if not question or not (2 <= len(options) <= 6):
        return text, None
    stripped = (text[: m.start()] + text[m.end():]).strip()
    return stripped, {
        "question": question,
        "options": options,
        "multi": bool(parsed.get("multi")),
    }


def _mentioned_files(text: str) -> list[Path]:
    """Existing workspace files referenced by absolute path in a reply."""
    found: list[Path] = []
    for raw in re.findall(re.escape(str(WORKSPACE_DIR)) + r"/[\w./\-]+", text):
        resolved = _resolve_workspace_path(raw.rstrip(".,;:"))
        if resolved and resolved.is_file() and resolved not in found:
            found.append(resolved)
        if len(found) >= 6:
            break
    return found


# Text of recently sent bot messages, keyed by message_id, so reaction
# handlers can recover what the user reacted to (reaction updates don't
# carry the message content). Bounded; stale entries mean "too old".
_SENT_CACHE: dict[int, str] = {}


def _remember_sent(message_id: int, text: str) -> None:
    if len(_SENT_CACHE) >= 30:
        for k in list(_SENT_CACHE)[:10]:
            _SENT_CACHE.pop(k, None)
    _SENT_CACHE[message_id] = text


_CODE_BLOCK_RE = re.compile(r"```\w*\n?(.*?)```", re.DOTALL)


def _copy_snippets(text: str, limit: int = 4) -> list[str]:
    """Fenced code blocks worth a one-tap copy button.

    Telegram's CopyTextButton caps the payload at 256 chars, so longer blocks
    are skipped (they'd be truncated silently otherwise)."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CODE_BLOCK_RE.finditer(text):
        snippet = match.group(1).strip()
        if snippet and len(snippet) <= 256 and snippet not in seen:
            seen.add(snippet)
            out.append(snippet)
        if len(out) >= limit:
            break
    return out


def _copy_keyboard(text: str) -> InlineKeyboardMarkup | None:
    rows = []
    for snippet in _copy_snippets(text):
        first_line = snippet.splitlines()[0]
        label = f"📋 {first_line[:28]}"
        if len(first_line) > 28 or "\n" in snippet:
            label += "…"
        rows.append(
            [InlineKeyboardButton(label, copy_text=CopyTextButton(snippet))]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def send_md(
    bot,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
    effect_id: str | None = None,
) -> None:
    """Send text as MarkdownV2, falling back to plain per chunk.

    The original text is split first and each chunk converted independently,
    so a chunk whose conversion Telegram rejects falls back to plain text on
    its own — earlier chunks are never re-sent. The split margin (3000 vs
    Telegram's 4096) leaves room for escape backslashes added by conversion;
    a chunk that still overflows after conversion is sent plain.

    Short fenced code blocks get one-tap copy buttons attached to the last
    chunk (native CopyTextButton — no callback round-trip). `thread_id`
    routes into a forum topic; `effect_id` plays a message effect on the
    first chunk (private chats only — silently retried without on failure).
    """
    if not text:
        text = "(no response)"
    chunks = split_for_telegram(text, max_len=3000)
    keyboard = _copy_keyboard(text)
    for i, chunk in enumerate(chunks):
        markup = keyboard if i == len(chunks) - 1 else None
        effect = effect_id if i == 0 and chat_id > 0 else None
        converted = to_telegram_markdown(chunk)
        payloads = []
        if len(converted) <= 4000:
            payloads.append({"text": converted, "parse_mode": "MarkdownV2"})
        payloads.append({"text": chunk})
        sent = None
        last_exc: Exception | None = None
        for payload in payloads:
            kwargs = {
                "chat_id": chat_id,
                "disable_web_page_preview": True,
                "reply_markup": markup,
                "message_thread_id": thread_id,
                **payload,
            }
            if effect:
                kwargs["message_effect_id"] = effect
            try:
                sent = await bot.send_message(**kwargs)
                break
            except Exception as e:
                last_exc = e
                log.warning("send failed (%s); falling back", e)
                effect = None  # an invalid effect id must not kill the send
        if sent is None:
            # every variant failed — callers (e.g. the notify inbox) need to
            # know delivery did not happen
            raise last_exc if last_exc else RuntimeError("send failed")
        _remember_sent(sent.message_id, chunk)


async def reply_md(update: Update, text: str) -> None:
    await send_md(update.get_bot(), update.effective_chat.id, text)


def _format_tool(tu: ToolUse) -> str:
    """Short Telegram-friendly label for a Claude tool_use block."""
    name = tu.name
    inp = tu.input or {}
    if name == "Bash":
        cmd = (inp.get("command") or "").splitlines()[0][:60]
        return f"🔧 Bash: {cmd}"
    if name == "Read":
        return f"📖 Read {Path(inp.get('file_path', '?')).name}"
    if name == "Write":
        return f"✏️ Write {Path(inp.get('file_path', '?')).name}"
    if name == "Edit":
        return f"✏️ Edit {Path(inp.get('file_path', '?')).name}"
    if name == "Glob":
        return f"🔍 Glob {inp.get('pattern', '?')[:50]}"
    if name == "Grep":
        return f"🔍 Grep {inp.get('pattern', '?')[:50]}"
    if name == "Task":
        return f"🤖 Subagent: {inp.get('subagent_type', 'task')}"
    if name == "WebFetch":
        return f"🌐 Fetch {(inp.get('url') or '')[:50]}"
    if name == "WebSearch":
        return f"🔍 Search: {(inp.get('query') or '')[:50]}"
    if name == "TodoWrite":
        return "📝 Todos updated"
    return f"🔧 {name}"


async def _react(
    bot,
    chat_id: int,
    message_id: int | None,
    emoji: str | None,
    is_big: bool = False,
) -> None:
    """Best-effort reaction on the user's message (👀 working, 👍 done, 💔
    error); is_big plays the enlarged animation (long-run celebration)."""
    if message_id is None:
        return
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)] if emoji else [],
            is_big=is_big or None,
        )
    except Exception as e:
        log.debug("reaction failed: %s", e)


async def run_claude_turn(
    bot,
    chat_id: int,
    uid: int,
    prompt: str,
    react_to: int | None = None,
    thread_id: int | None = None,
    session: str | None = None,
) -> str | None:
    """Run one Claude turn, streaming progress into a status message.

    The status message shows the partial assistant text as it arrives (tail,
    plain text) and the latest tool-use label; edits are throttled to one per
    1.5s to stay under Telegram's edit rate limit. When Claude returns, the
    status is deleted and the full reply is sent via send_md(), followed by
    download buttons for any workspace files it mentions and a trailing
    TGQUESTION rendered as inline buttons (or a native poll when multi).
    `thread_id` keeps everything inside a forum topic; `session` runs the
    turn on a specific named session (topics map to their own).
    """
    started = time.monotonic()
    await _react(bot, chat_id, react_to, "👀")
    if claude.busy(uid):
        status = await bot.send_message(
            chat_id=chat_id,
            text="📥 Queued — Claude is still working on the previous message…",
            message_thread_id=thread_id,
        )
    else:
        status = await bot.send_message(
            chat_id=chat_id, text="🤔 thinking…", message_thread_id=thread_id
        )
    state = {"last_edit": 0.0, "text": "", "tool": ""}

    async def _refresh() -> None:
        now = time.monotonic()
        if now - state["last_edit"] < 1.5:
            return
        state["last_edit"] = now
        parts = []
        if state["text"]:
            tail = state["text"][-900:]
            parts.append(("…" if len(state["text"]) > 900 else "") + tail)
        if state["tool"]:
            parts.append(state["tool"])
        body = "\n\n".join(parts) or "🤔 thinking…"
        try:
            await status.edit_text(body[:4000])
        except Exception as e:
            log.debug("progress edit failed: %s", e)

    async def on_tool(tu: ToolUse) -> None:
        state["tool"] = _format_tool(tu)
        await _refresh()

    async def on_text(chunk: str) -> None:
        state["text"] = (state["text"] + "\n\n" + chunk).strip()
        state["tool"] = ""
        await _refresh()

    ask_kwargs = {"on_tool_use": on_tool, "on_text": on_text}
    if session:
        ask_kwargs["session"] = session
    try:
        resp = await claude.ask(uid, prompt, **ask_kwargs)
    except ClaudeSessionError as e:
        log.error("claude turn: session error: %s", e)
        await _react(bot, chat_id, react_to, "💔")
        try:
            await status.edit_text(session_error_reply(e))
        except Exception:
            pass
        return None
    except Exception as e:
        log.exception("claude turn: ask failed")
        await _react(bot, chat_id, react_to, "💔")
        try:
            await status.edit_text(f"Error: {e}")
        except Exception:
            pass
        return None

    try:
        await status.delete()
    except Exception:
        pass

    long_run = time.monotonic() - started > LONG_RUN_S
    resp, tgq = _extract_tgquestion(resp)
    if resp or not tgq:
        await send_md(
            bot,
            chat_id,
            resp,
            thread_id=thread_id,
            effect_id=EFFECT_TADA_ID if long_run else None,
        )

    files = _mentioned_files(resp)
    if files:
        keyboard = [
            [InlineKeyboardButton(
                f"📎 {p.name}"[:60],
                callback_data=f"fget:{_cb_store(str(p))}",
            )]
            for p in files
        ]
        await bot.send_message(
            chat_id=chat_id,
            text="Mentioned files:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            message_thread_id=thread_id,
        )

    if tgq:
        await _send_tgquestion(bot, chat_id, uid, tgq, thread_id)

    await _react(bot, chat_id, react_to, "👍", is_big=long_run)
    return resp


async def _send_tgquestion(
    bot, chat_id: int, uid: int, tgq: dict, thread_id: int | None = None
) -> None:
    """Render a TGQUESTION: inline buttons for single choice, a native
    non-anonymous poll for multi-select (answers come back via PollAnswer)."""
    if tgq.get("multi"):
        try:
            msg = await bot.send_poll(
                chat_id=chat_id,
                question=tgq["question"][:300],
                options=[o[:100] for o in tgq["options"]],
                is_anonymous=False,
                allows_multiple_answers=True,
                message_thread_id=thread_id,
            )
            _CB_CACHE[msg.poll.id] = (
                "poll", tgq["question"], tgq["options"], chat_id, uid,
                thread_id, msg.message_id,
            )
            return
        except Exception as e:
            log.warning("poll send failed (%s); falling back to buttons", e)
    token = _cb_store((tgq["question"], tgq["options"], chat_id, uid, thread_id))
    keyboard = [
        [InlineKeyboardButton(opt, callback_data=f"tgq:{token}:{i}")]
        for i, opt in enumerate(tgq["options"])
    ]
    await bot.send_message(
        chat_id=chat_id,
        text="❓ " + tgq["question"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=thread_id,
    )


def _topic_info(update: Update) -> tuple[int | None, str | None]:
    """(thread_id, session_name) for forum-topic messages, (None, None)
    elsewhere. Each topic gets its own deterministic named session, so a
    forum group gives you one Claude conversation per topic for free."""
    msg = update.effective_message
    if msg is None or not getattr(msg, "is_topic_message", False):
        return None, None
    thread_id = getattr(msg, "message_thread_id", None)
    if not thread_id:
        return None, None
    return thread_id, f"topic-{thread_id}"


async def claude_reply(update: Update, prompt: str) -> str | None:
    thread_id, session = _topic_info(update)
    return await run_claude_turn(
        update.get_bot(),
        update.effective_chat.id,
        update.effective_user.id,
        prompt,
        react_to=update.message.message_id if update.message else None,
        thread_id=thread_id,
        session=session,
    )


async def check_notify(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver Claude's proactive messages: any file dropped into
    workspace/.notify/ is sent to the user's chat and removed. Runs every few
    seconds via the JobQueue. Files that fail to send are renamed *.failed so
    they don't loop."""
    notify_dir = WORKSPACE_DIR / ".notify"
    if not notify_dir.is_dir():
        return
    chat_id = _load_chat_id()
    if chat_id is None:
        return  # nobody has talked to the bot yet; leave files for later
    try:
        paths = sorted(
            (p for p in notify_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as e:
        log.warning("notify scan failed: %s", e)
        return
    for path in paths:
        if path.name.startswith(".") or path.suffix == ".failed":
            continue
        try:
            text = path.read_text(errors="replace").strip()[:8000]
        except Exception as e:
            log.warning("notify read failed for %s: %s", path.name, e)
            continue
        # A JSON payload with options becomes an actionable notification:
        # the answer flows back to Claude like a TGQUESTION reply.
        payload = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict) and isinstance(data.get("options"), list):
                    payload = data
            except Exception:
                payload = None
        try:
            if payload is not None:
                body = str(payload.get("text") or "").strip()
                question = str(payload.get("question") or "").strip()
                options = [
                    str(o).strip()[:60]
                    for o in payload["options"]
                    if str(o).strip()
                ]
                if body:
                    await send_md(ctx.bot, chat_id, "🔔 " + body)
                if question and 2 <= len(options) <= 6:
                    await _send_tgquestion(
                        ctx.bot,
                        chat_id,
                        ALLOWED_USER,
                        {
                            "question": question,
                            "options": options,
                            "multi": bool(payload.get("multi")),
                        },
                    )
                elif not body:
                    await send_md(ctx.bot, chat_id, "🔔 " + text)
            elif text:
                await send_md(ctx.bot, chat_id, "🔔 " + text)
            path.unlink()
        except Exception as e:
            log.warning("notify delivery failed for %s: %s", path.name, e)
            try:
                path.rename(path.with_name(path.name + ".failed"))
            except Exception:
                pass


async def check_schedule(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Register persistent jobs that Claude drops into workspace/.schedule/.

    Each file is a JSON object:
        {"cron": "<5-field cron>", "prompt": "<text>",
         "model": <optional>, "once": <optional bool>}

    Mirrors check_notify: polled every few seconds via the JobQueue, so Claude
    can schedule work that survives sessions AND bot restarts without the user
    typing /schedule. Bad files are renamed *.failed so they don't loop."""
    sched_dir = WORKSPACE_DIR / ".schedule"
    if not sched_dir.is_dir() or scheduler is None:
        return
    chat_id = _load_chat_id()
    if chat_id is None:
        return  # nobody has talked to the bot yet; leave files for later
    try:
        paths = sorted(
            (p for p in sched_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as e:
        log.warning("schedule scan failed: %s", e)
        return
    for path in paths:
        if path.name.startswith(".") or path.suffix == ".failed":
            continue

        def _fail(reason: str) -> None:
            log.warning("schedule %s rejected: %s", path.name, reason)
            try:
                path.rename(path.with_name(path.name + ".failed"))
            except Exception:
                pass

        try:
            data = json.loads(path.read_text(errors="replace"))
        except Exception as e:
            _fail(f"bad JSON: {e}")
            continue
        if not isinstance(data, dict):
            _fail("not a JSON object")
            continue
        cron_expr = str(data.get("cron") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        once = bool(data.get("once"))
        model = data.get("model")
        if isinstance(model, str):
            model = model.strip() or None
            if model and not _MODEL_NAME_RE.match(model):
                model = None
        else:
            model = None
        if not cron_expr or not prompt:
            _fail("missing cron or prompt")
            continue
        try:
            job_id = scheduler.add(
                cron=cron_expr,
                prompt=prompt,
                chat_id=chat_id,
                uid=ALLOWED_USER,
                model=model,
                once=once,
            )
        except ValueError as e:
            try:
                await send_md(ctx.bot, chat_id, f"⚠️ Cron non valido ({cron_expr}): {e}")
            except Exception:
                pass
            _fail(f"invalid cron: {e}")
            continue
        try:
            path.unlink()
        except Exception:
            pass
        kind = "una volta" if once else "ricorrente"
        suffix = f" [{model}]" if model else ""
        try:
            await send_md(
                ctx.bot,
                chat_id,
                f"🗓 Programmato ({kind}) {cron_expr}{suffix} — id {job_id}\n"
                + prompt[:200],
            )
        except Exception as e:
            log.warning("schedule confirm failed: %s", e)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    _save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "Bot ready. Send a message to talk to Claude Code.\nUse /help for commands."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "/new - new conversation (current session)\n"
        "/session [name|del name] - list/switch/create/delete named sessions\n"
        "/cancel - kill the current Claude run\n"
        "/compact - compact context\n"
        "/status [pin|unpin] - status; pin keeps a live card pinned\n"
        "/quick [add|rm|hide] - persistent keyboard with your prompts\n"
        "/logs [n] - last n bot log lines\n"
        "/auth - check Claude auth status\n"
        "/login - sign in via browser link (or paste credentials JSON)\n"
        "/file <query> - retrieve files matching a query (Claude searches)\n"
        "/get <path> - send a workspace file by path (no Claude)\n"
        "/img [n] <prompt> - generate image(s), n=2-4 as album (OpenAI)\n"
        "/export - download the session transcript\n"
        "/model [name] - show or set Claude model\n"
        "React to my messages: 👍 go ahead, 👎 reconsider, ❤/🔥/💯 save to workspace/saved/\n"
        "Reply (or quote-reply) to any message to give Claude that exact context\n"
        "Edit a sent prompt to get a one-tap re-run with the corrected text\n"
        "In a forum group, each topic is its own session\n"
        '/schedule add "<cron>" [model=<m>] <prompt> - recurring prompt\n'
        "/schedule list - list jobs\n"
        "/schedule remove <id> - remove job\n"
        "/help - this list"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await claude.reset(update.effective_user.id)
    await update.message.reply_text(
        f"New session started ({claude.current_name})."
    )


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    uid = update.effective_user.id
    if ctx.args:
        sub = ctx.args[0].strip().lower()
        if sub == "del":
            if len(ctx.args) < 2:
                await update.message.reply_text("Usage: /session del <name>")
                return
            name = ctx.args[1].strip().lower()
            if await claude.delete(uid, name):
                await update.message.reply_text(
                    f"Session '{name}' deleted. Current: {claude.current_name}"
                )
            else:
                await update.message.reply_text(f"No session named '{name}'.")
            return
        name = sub
        if not _SESSION_NAME_RE.match(name):
            await update.message.reply_text(
                "Invalid name: use a-z, 0-9, - and _, max 32 chars."
            )
            return
        await claude.switch(uid, name)
        await update.message.reply_text(f"Session: {name}")
        return
    names = claude.session_names()
    keyboard = [
        [InlineKeyboardButton(
            f"{'• ' if n == claude.current_name else ''}{n}",
            callback_data=f"session:{n}",
        )]
        for n in names
    ]
    await update.message.reply_text(
        f"Current session: {claude.current_name}\n"
        "Tap to switch, or /session <name> to create, /session del <name> to delete.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_session_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    name = query.data.split(":", 1)[1]
    if not _SESSION_NAME_RE.match(name):
        await query.answer("Invalid session.")
        return
    await claude.switch(update.effective_user.id, name)
    await query.answer(f"Switched to {name}")
    try:
        await query.edit_message_text(f"Session: {name}")
    except Exception:
        pass


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if claude.cancel(update.effective_user.id):
        await update.message.reply_text(
            "⏹ Cancelled the current run. Queued messages (if any) still run — "
            "send /cancel again to kill the next one."
        )
    else:
        await update.message.reply_text("Nothing is running.")


_COMPACT_SUMMARY_PROMPT = (
    "Summarize this conversation for context compaction. Keep: current goals, "
    "important decisions, key facts (paths, commands, names), and any "
    "unfinished work. Be concise but complete. Reply with the summary only."
)


async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Compact the main session: summarize it, reset, seed a fresh one.

    `claude -p` can't reliably run the interactive /compact slash command, so
    compaction is done explicitly: ask the current session for a summary,
    drop it, and start a new session primed with that summary.
    """
    if not allowed(update):
        return
    if not is_authed():
        await update.message.reply_text("Claude not authenticated. Use /login.")
        return
    uid = update.effective_user.id
    status = await update.message.reply_text("📦 Compacting context…")
    try:
        summary = await claude.ask(uid, _COMPACT_SUMMARY_PROMPT)
        await claude.reset(uid)
        await claude.ask(
            uid,
            "This fresh session is seeded with the compacted summary of the "
            "previous conversation:\n\n" + summary +
            "\n\nAcknowledge with one short sentence.",
        )
    except ClaudeSessionError as e:
        log.error("compact failed: %s", e)
        try:
            await status.edit_text(session_error_reply(e))
        except Exception:
            pass
        return
    except Exception as e:
        log.exception("compact failed")
        try:
            await status.edit_text(f"Compact failed: {e}")
        except Exception:
            pass
        return
    await status.edit_text("Context compacted into a fresh session.")


def _load_quick() -> list[str]:
    try:
        items = json.loads(QUICK_FILE.read_text())
        return [str(i)[:40] for i in items if str(i).strip()][:12]
    except Exception:
        return []


def _save_quick(items: list[str]) -> None:
    QUICK_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUICK_FILE.write_text(json.dumps(items, ensure_ascii=False))


def _quick_keyboard() -> ReplyKeyboardMarkup | None:
    items = _load_quick()
    if not items:
        return None
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


async def cmd_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Persistent reply keyboard with the user's recurring prompts.

    Tapping a button just sends its text, which flows to Claude like any
    typed message — zero extra plumbing."""
    if not allowed(update):
        return
    args = ctx.args or []
    if args and args[0] == "add":
        text = " ".join(args[1:]).strip()[:40]
        if not text:
            await update.message.reply_text("Usage: /quick add <prompt>")
            return
        items = _load_quick()
        if text in items:
            await update.message.reply_text("Already on the keyboard.")
            return
        if len(items) >= 12:
            await update.message.reply_text("Keyboard full (12 max). /quick rm <n> first.")
            return
        items.append(text)
        _save_quick(items)
        await update.message.reply_text(
            f"Added. ({len(items)}/12)", reply_markup=_quick_keyboard()
        )
        return
    if args and args[0] == "rm":
        items = _load_quick()
        try:
            idx = int(args[1]) - 1
            removed = items.pop(idx)
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /quick rm <number> (see /quick)")
            return
        _save_quick(items)
        markup = _quick_keyboard() or ReplyKeyboardRemove()
        await update.message.reply_text(f"Removed: {removed}", reply_markup=markup)
        return
    if args and args[0] == "hide":
        await update.message.reply_text(
            "Keyboard hidden. /quick to bring it back.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    items = _load_quick()
    if not items:
        await update.message.reply_text(
            "No quick prompts yet. Add one with: /quick add <prompt>"
        )
        return
    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
    await update.message.reply_text(
        f"Quick prompts:\n{listing}\n\n/quick add <prompt> · /quick rm <n> · /quick hide",
        reply_markup=_quick_keyboard(),
    )


BOT_STARTED = time.time()


def _fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _status_text() -> str:
    info = claude.status_info()
    lines = [
        f"Auth: {'✅' if is_authed() else '❌ — use /login'}",
        f"Model: {load_model()}",
        f"Session: {info['session_name']} ({info['session_id'] or 'no id yet'})",
    ]
    if info["busy_since"]:
        lines.append(f"Running: yes, for {_fmt_secs(time.time() - info['busy_since'])}")
    else:
        lines.append("Running: no")
    last = info["last_result"]
    if last:
        parts = []
        cost = transcript.cost_usd(last)
        if cost is not None:
            parts.append(f"${cost:.4f}")
        dur = transcript.duration_ms(last)
        if dur:
            parts.append(_fmt_secs(dur / 1000))
        usage = transcript.usage(last)
        tok_in = usage.get("input_tokens")
        tok_out = usage.get("output_tokens")
        if tok_in is not None or tok_out is not None:
            parts.append(f"{tok_in or 0}→{tok_out or 0} tok")
        if parts:
            lines.append("Last turn: " + ", ".join(parts))
    lines.append(f"Session cost: ${info['total_cost_usd']:.4f}")
    lines.append(f"Bot uptime: {_fmt_secs(time.time() - BOT_STARTED)}")
    return "\n".join(lines)


def _load_pin() -> dict | None:
    try:
        data = json.loads(PIN_FILE.read_text())
        return data if isinstance(data, dict) and "message_id" in data else None
    except Exception:
        return None


_last_pin_text = {"text": ""}


async def update_pinned(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the pinned status card fresh (JobQueue, every 30s)."""
    pin = _load_pin()
    if pin is None:
        return
    text = "📌 " + _status_text()
    if text == _last_pin_text["text"]:
        return
    try:
        await ctx.bot.edit_message_text(
            chat_id=pin["chat_id"], message_id=pin["message_id"], text=text
        )
        _last_pin_text["text"] = text
    except Exception as e:
        log.debug("pinned status update failed: %s", e)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    args = ctx.args or []
    if args and args[0] == "pin":
        msg = await update.message.reply_text("📌 " + _status_text())
        try:
            await ctx.bot.pin_chat_message(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as e:
            await update.message.reply_text(f"Could not pin: {e}")
            return
        PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PIN_FILE.write_text(json.dumps(
            {"chat_id": update.effective_chat.id, "message_id": msg.message_id}
        ))
        _last_pin_text["text"] = ""
        return
    if args and args[0] == "unpin":
        pin = _load_pin()
        if pin:
            try:
                await ctx.bot.unpin_chat_message(
                    chat_id=pin["chat_id"], message_id=pin["message_id"]
                )
            except Exception as e:
                log.debug("unpin failed: %s", e)
            try:
                PIN_FILE.unlink()
            except OSError:
                pass
            await update.message.reply_text("Status unpinned.")
        else:
            await update.message.reply_text("Nothing pinned.")
        return
    await update.message.reply_text(_status_text())


async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if is_authed():
        await update.message.reply_text("Claude is authenticated. Use /login to re-auth.")
    else:
        await update.message.reply_text("Claude is not authenticated. Use /login.")


# One pending browser login at a time (single-user bot).
_PENDING_LOGIN: dict = {"login": None}


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    login = oauth.new_login()
    _PENDING_LOGIN["login"] = login
    await update.message.reply_text(
        "🔐 Sign in with your browser:\n"
        "1. Open the link below\n"
        "2. Log into claude.ai (email + code)\n"
        "3. Approve the authorization\n"
        "4. Copy the code shown and paste it here\n\n"
        "The link expires in 15 minutes.\n\n"
        "Fallback: paste the credentials JSON from an authenticated machine "
        "(macOS: `security find-generic-password -s \"Claude Code-credentials\" -w`, "
        "Linux: `cat ~/.claude/.credentials.json`).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔐 Accedi a Claude", url=login["url"])]]
        ),
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if ctx.args:
        # Free-text escape hatch for models not on the button list; the CLI
        # validates the actual name on the next turn.
        name = ctx.args[0].strip()
        if not _MODEL_NAME_RE.match(name):
            await update.message.reply_text(
                "Invalid model name. Use /model and pick from the list, or "
                "pass a plain model id (letters, digits, . - _ [ ])."
            )
            return
        save_model(name)
        await update.message.reply_text(
            f"Model set to {name}. (Switching model resets the session.)"
        )
        return
    current = load_model()
    keyboard = [
        [InlineKeyboardButton(
            f"{'• ' if value == current else ''}{label}",
            callback_data=f"model:{value}",
        )]
        for label, value in MODEL_CHOICES
    ]
    await update.message.reply_text(
        f"Current: {current}\nChoose a model (switching resets the session):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_fget_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    token = query.data.split(":", 1)[1]
    payload = _CB_CACHE.get(token)
    if not isinstance(payload, str):
        await query.answer("Expired — use /get <path>.")
        return
    await query.answer()
    await _send_file_to(ctx.bot, update.effective_chat.id, Path(payload))


async def on_tgq_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    try:
        _, token, idx_raw = query.data.split(":")
        idx = int(idx_raw)
    except ValueError:
        await query.answer("Bad data.")
        return
    payload = _CB_CACHE.pop(token, None)
    if not isinstance(payload, tuple):
        await query.answer("Expired — type your answer instead.")
        return
    question, options, chat_id, uid, thread_id = payload
    if not 0 <= idx < len(options):
        await query.answer("Bad option.")
        return
    choice = options[idx]
    await query.answer()
    try:
        await query.edit_message_text(f"❓ {question}\n➡️ {choice}")
    except Exception:
        pass
    await run_claude_turn(ctx.bot, chat_id, uid, choice, thread_id=thread_id)


async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Multi-select TGQUESTION answers come back as PollAnswer updates."""
    ans = update.poll_answer
    if ans is None or ans.user is None or ans.user.id != ALLOWED_USER:
        return
    payload = _CB_CACHE.pop(ans.poll_id, None)
    if not (isinstance(payload, tuple) and payload and payload[0] == "poll"):
        return
    _, question, options, chat_id, uid, thread_id, message_id = payload
    chosen = [options[i] for i in ans.option_ids if 0 <= i < len(options)]
    if not chosen:
        return
    try:
        await ctx.bot.stop_poll(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.debug("stop_poll failed: %s", e)
    await run_claude_turn(
        ctx.bot,
        chat_id,
        uid,
        f'Answer to "{question}": {", ".join(chosen)}',
        thread_id=thread_id,
    )


async def on_model_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith("model:"):
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    name = query.data.split(":", 1)[1]
    if name not in MODEL_VALUES:
        await query.answer("Invalid model.")
        return
    save_model(name)
    await query.answer(f"Set to {name}")
    await query.edit_message_text(f"Model set to {name}.")


async def cmd_get(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /get <path>\nPath relative to /workspace, or absolute under /workspace."
        )
        return
    raw = " ".join(ctx.args).strip()
    resolved = _resolve_workspace_path(raw)
    if resolved is None:
        await update.message.reply_text(f"Path must be under {WORKSPACE_DIR}.")
        return
    await _send_file(update, resolved)


async def cmd_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /file <description of files to retrieve>"
        )
        return

    if not is_authed():
        await update.message.reply_text("Claude not authenticated. Use /login.")
        return

    query = " ".join(ctx.args).strip()
    full_prompt = _FILE_SEARCH_SYSTEM + "\n\nUser request: " + query

    log.info("file-search [isolated] query=%s", query[:200])
    await update.message.reply_chat_action("typing")

    try:
        result_text = await claude.ask_isolated(full_prompt)
    except ClaudeSessionError as e:
        log.error("file-search failed: %s", e)
        await update.message.reply_text(session_error_reply(e))
        return
    except Exception as e:
        log.exception("file-search failed")
        await update.message.reply_text(f"Claude error: {e}")
        return

    parsed = _extract_json_block(result_text)
    if not isinstance(parsed, dict):
        await update.message.reply_text(
            "Could not parse file list from Claude.\n\n" + result_text[:1500]
        )
        return

    files = parsed.get("files") or []
    note = parsed.get("note")

    if not files:
        msg = "No matching files found."
        if note:
            msg += f"\n\n{note}"
        await update.message.reply_text(msg)
        return

    if note:
        await update.message.reply_text(note)

    for entry in files:
        if not isinstance(entry, dict):
            continue
        path_raw = entry.get("path", "")
        title = entry.get("title") or Path(path_raw).name
        resolved = _resolve_workspace_path(path_raw)
        if resolved is None:
            await update.message.reply_text(f"Path outside workspace: {path_raw}")
            continue
        await _send_file(update, resolved, caption=title)


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            '/schedule add "<cron>" <prompt>\n'
            "/schedule list\n"
            "/schedule remove <id>"
        )
        return

    sub = args[0]

    if sub == "list":
        jobs = scheduler.list()
        if not jobs:
            await update.message.reply_text("No scheduled jobs.")
            return
        lines = [
            f"{j['id']}: {j['cron']!r}"
            + (f" [{j['model']}]" if j.get("model") else "")
            + f" -> {j['prompt'][:80]}"
            for j in jobs
        ]
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /schedule remove <id>")
            return
        ok = scheduler.remove(args[1])
        await update.message.reply_text("Removed." if ok else "Job not found.")
        return

    if sub == "add":
        rest = update.message.text.split(maxsplit=2)
        if len(rest) < 3:
            await update.message.reply_text('Usage: /schedule add "<cron>" <prompt>')
            return
        body = rest[2]
        if body.startswith('"'):
            end = body.find('"', 1)
            if end == -1:
                await update.message.reply_text("Missing closing quote.")
                return
            cron_expr = body[1:end]
            prompt = body[end + 1:].strip()
        else:
            parts = body.split(maxsplit=5)
            if len(parts) < 6:
                await update.message.reply_text('Usage: /schedule add "<cron>" <prompt>')
                return
            cron_expr = " ".join(parts[:5])
            prompt = parts[5]
        # Optional per-job model routing: /schedule add "<cron>" model=haiku <prompt>
        job_model = None
        if prompt.startswith("model="):
            head, _, rest = prompt.partition(" ")
            candidate = head[len("model="):]
            if candidate and _MODEL_NAME_RE.match(candidate) and rest.strip():
                job_model = candidate
                prompt = rest.strip()
        if not prompt:
            await update.message.reply_text("Empty prompt.")
            return
        try:
            job_id = scheduler.add(
                cron=cron_expr,
                prompt=prompt,
                chat_id=update.effective_chat.id,
                uid=update.effective_user.id,
                model=job_model,
            )
        except ValueError as e:
            await update.message.reply_text(f"Invalid cron: {e}")
            return
        suffix = f" (model {job_model})" if job_model else ""
        await update.message.reply_text(f"Scheduled job {job_id}{suffix}.")
        return

    await update.message.reply_text(f"Unknown subcommand: {sub}")


def _safe_filename(name: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


async def handle_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if update.message is None:
        return
    _save_chat_id(update.effective_chat.id)
    msg = update.message

    obj = None
    default_ext = ""
    if msg.document:
        obj = msg.document
    elif msg.photo:
        obj = msg.photo[-1]
        default_ext = ".jpg"
    elif msg.video:
        obj = msg.video
        default_ext = ".mp4"
    elif msg.animation:
        obj = msg.animation
        default_ext = ".mp4"
    else:
        return

    file_size = getattr(obj, "file_size", 0) or 0
    if file_size > MAX_DOWNLOAD_BYTES:
        await msg.reply_text(
            f"File too large ({file_size // (1024 * 1024)} MB). "
            "Telegram bot API caps downloads at 20 MB."
        )
        return

    await msg.reply_chat_action("typing")

    inbox = WORKSPACE_DIR / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)

    raw_name = getattr(obj, "file_name", None) or f"{type(obj).__name__.lower()}{default_ext}"
    safe = _safe_filename(raw_name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = inbox / f"{timestamp}_{safe}"

    try:
        file = await ctx.bot.get_file(obj.file_id)
        await file.download_to_drive(custom_path=str(target))
    except Exception as e:
        log.exception("Upload download failed")
        await msg.reply_text(f"Failed to download file: {e}")
        return

    log.info("Saved upload to %s (%d bytes)", target, file_size)

    if not is_authed():
        await msg.reply_text(
            f"File saved to {target}.\nClaude not authenticated; use /login to process it."
        )
        return

    caption = (msg.caption or "").strip()
    if caption:
        prompt = f"The user uploaded a file at `{target}`.\nCaption: {caption}"
    else:
        prompt = (
            f"The user uploaded a file at `{target}` with no caption. "
            "Briefly acknowledge what the file is and wait for instructions."
        )

    await claude_reply(update, prompt)


async def _transcribe(audio_path: Path) -> str:
    loop = asyncio.get_running_loop()

    def _run() -> str:
        with audio_path.open("rb") as fh:
            resp = _openai_client.audio.transcriptions.create(
                model=WHISPER_MODEL, file=fh
            )
        return resp.text

    return await loop.run_in_executor(None, _run)


def _speakable(text: str) -> str:
    """Strip markdown noise that sounds terrible when read aloud."""
    text = re.sub(r"```.*?```", " (codice) ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]\([^)\n]+\)", r"\1", text)
    text = re.sub(r"[*_#~]+", "", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


async def _tts(text: str) -> bytes:
    loop = asyncio.get_running_loop()

    def _run() -> bytes:
        resp = _openai_client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text[:4000],
            response_format="opus",
        )
        return resp.content

    return await loop.run_in_executor(None, _run)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if update.message is None:
        return
    _save_chat_id(update.effective_chat.id)
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    file_size = getattr(voice, "file_size", 0) or 0
    if file_size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"File too large ({file_size // (1024 * 1024)} MB). "
            "Telegram bot API caps downloads at 20 MB."
        )
        return

    await update.message.reply_chat_action("typing")

    file = await ctx.bot.get_file(voice.file_id)
    suffix = ".ogg" if update.message.voice else Path(getattr(voice, "file_name", "") or "audio.bin").suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        await file.download_to_drive(custom_path=str(tmp_path))
        try:
            transcribed = (await _transcribe(tmp_path)).strip()
        except Exception as e:
            log.exception("Whisper failed")
            await update.message.reply_text(f"Transcription error: {e}")
            return
        if not transcribed:
            await update.message.reply_text("Empty transcription.")
            return

        await update.message.reply_text(f"(voice) {transcribed}")

        if not is_authed():
            await update.message.reply_text("Claude not authenticated. Use /login.")
            return

        resp = await claude_reply(update, transcribed)

        # Voice in, voice out: speak the reply too (TGCR_TTS=0 disables).
        if resp and TTS_ENABLED:
            spoken = _speakable(resp)
            if spoken:
                try:
                    audio = await _tts(spoken)
                    await update.message.reply_voice(audio)
                except Exception as e:
                    log.warning("TTS failed: %s", e)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _with_reply_context(msg, text: str) -> str:
    """Prepend the replied-to (or precisely quoted) text as explicit context.

    Telegram lets the user quote a *part* of a message when replying — that
    slice beats the whole message for precision. For plain replies, prefer
    our sent-text cache (bot messages) and fall back to the message's own
    text/caption. Topic messages are technically replies to the topic
    service message — those don't count."""
    quoted = None
    quote = getattr(msg, "quote", None)
    if quote is not None and getattr(quote, "text", None):
        quoted = quote.text
    else:
        replied = getattr(msg, "reply_to_message", None)
        if replied is not None and getattr(replied, "forum_topic_created", None):
            replied = None
        if replied is not None:
            quoted = (
                _SENT_CACHE.get(replied.message_id)
                or getattr(replied, "text", None)
                or getattr(replied, "caption", None)
            )
    if not quoted:
        return text
    quoted = quoted.strip()[:1000]
    return (
        "[The user is replying to this earlier message:]\n"
        f'"""\n{quoted}\n"""\n\n{text}'
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if update.message is None or update.message.text is None:
        return
    _save_chat_id(update.effective_chat.id)
    text = update.message.text.strip()

    # Pending browser login: the pasted authorization code gets exchanged
    # for tokens (see bot/oauth.py). The bot never sees email/password.
    pending = _PENDING_LOGIN.get("login")
    if pending is not None and oauth.looks_like_code(text):
        if oauth.is_expired(pending):
            _PENDING_LOGIN["login"] = None
            await update.message.reply_text("Login expired — run /login again.")
            return
        try:
            creds = await oauth.exchange_code(text, pending)
        except Exception as e:
            log.warning("oauth exchange failed: %s", e)
            await update.message.reply_text(
                f"Login failed: {e}\nOpen the /login link and try a fresh code."
            )
            return
        _PENDING_LOGIN["login"] = None
        save_credentials_json(json.dumps(creds))
        try:
            await update.message.delete()
        except Exception as e:
            log.debug("could not delete code message: %s", e)
        await update.effective_chat.send_message("✅ Logged in. Claude is ready.")
        return

    if text.startswith("{") and ("claudeAiOauth" in text or "accessToken" in text):
        try:
            save_credentials_json(text)
        except Exception as e:
            await update.message.reply_text(f"Invalid JSON: {e}")
            return
        # Don't leave the OAuth token sitting in the chat history.
        try:
            await update.message.delete()
            note = "Credentials saved (your message was deleted for safety). Claude is ready."
        except Exception as e:
            log.warning("could not delete credentials message: %s", e)
            note = (
                "Credentials saved. Claude is ready.\n"
                "⚠️ Could not delete your message — remove it manually, "
                "it contains your OAuth token."
            )
        await update.effective_chat.send_message(note)
        return

    if not is_authed():
        await update.message.reply_text("Claude not authenticated. Use /login.")
        return

    await claude_reply(update, _with_reply_context(update.message, text))


async def on_edited(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """The user fixed a typo in an already-sent prompt: offer a one-tap
    re-run with the corrected text instead of silently ignoring the edit."""
    msg = update.edited_message
    if (
        msg is None
        or update.effective_user is None
        or update.effective_user.id != ALLOWED_USER
    ):
        return
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    thread_id, session = _topic_info(update)
    token = _cb_store(
        ("rerun", text, msg.chat_id, update.effective_user.id, thread_id, session)
    )
    await msg.reply_text(
        "✏️ Message edited — re-run with the corrected text?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Re-run", callback_data=f"rerun:{token}")]]
        ),
    )


async def on_rerun_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    token = query.data.split(":", 1)[1]
    payload = _CB_CACHE.pop(token, None)
    if not (isinstance(payload, tuple) and payload and payload[0] == "rerun"):
        await query.answer("Expired.")
        return
    _, text, chat_id, uid, thread_id, session = payload
    await query.answer()
    try:
        await query.edit_message_text("🔁 Re-running with the corrected text…")
    except Exception:
        pass
    await run_claude_turn(
        ctx.bot,
        chat_id,
        uid,
        "(corrected message) " + text,
        thread_id=thread_id,
        session=session,
    )


def _transcript_markdown(sid: str) -> str | None:
    """Render the Claude Code session transcript jsonl as readable markdown."""
    projects = CLAUDE_HOME / "projects"
    if not projects.is_dir():
        return None
    matches = list(projects.glob(f"*/{sid}.jsonl"))
    if not matches:
        return None
    sections: list[str] = []
    for raw in matches[0].read_text(errors="replace").splitlines():
        try:
            entry = json.loads(raw)
        except Exception:
            continue
        etype = entry.get("type")
        message = entry.get("message") or {}
        content = message.get("content")
        if etype == "user":
            if isinstance(content, str):
                texts = [content]
            else:
                texts = [
                    b.get("text", "")
                    for b in (content or [])
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
            text = "\n".join(t for t in texts if t).strip()
            if text:
                sections.append("## 👤 User\n\n" + text)
        elif etype == "assistant":
            parts = []
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    parts.append(f"> 🔧 {b.get('name', 'tool')}")
            text = "\n\n".join(parts).strip()
            if text:
                sections.append("## 🤖 Claude\n\n" + text)
    if not sections:
        return None
    return f"# Session {sid}\n\n" + "\n\n---\n\n".join(sections) + "\n"


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    sid = claude.status_info()["session_id"]
    if not sid:
        await update.message.reply_text("No active session to export.")
        return
    doc = _transcript_markdown(sid)
    if doc is None:
        await update.message.reply_text(f"Transcript for session {sid} not found.")
        return
    buf = BytesIO(doc.encode())
    buf.name = f"session-{sid[:8]}.md"
    await update.message.reply_document(buf, filename=buf.name)


async def _gen_images(prompt: str, n: int = 1) -> list:
    loop = asyncio.get_running_loop()

    def _decode(data) -> bytes | str:
        if getattr(data, "b64_json", None):
            return base64.b64decode(data.b64_json)
        return data.url

    def _run() -> list:
        # dall-e models return a URL unless asked for b64 and only accept
        # n=1; gpt-image-1 is always b64, rejects response_format, and
        # generates n variants in one call.
        if IMAGE_MODEL.startswith("dall-e"):
            out = []
            for _ in range(n):
                resp = _openai_client.images.generate(
                    model=IMAGE_MODEL,
                    prompt=prompt,
                    size="1024x1024",
                    n=1,
                    response_format="b64_json",
                )
                out.append(_decode(resp.data[0]))
            return out
        resp = _openai_client.images.generate(
            model=IMAGE_MODEL, prompt=prompt, size="1024x1024", n=n
        )
        return [_decode(d) for d in resp.data]

    return await loop.run_in_executor(None, _run)


async def _gen_image(prompt: str) -> bytes | str:
    return (await _gen_images(prompt, 1))[0]


async def cmd_img(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if _openai_client is None:
        await update.message.reply_text("Image generation needs OPENAI_API_KEY in .env.")
        return
    args = list(ctx.args or [])
    count = 1
    if args and args[0].isdigit():
        count = max(1, min(int(args[0]), 4))
        args = args[1:]
    prompt = " ".join(args).strip()
    if not prompt:
        await update.message.reply_text(
            "Usage: /img [n] <description>  (n = 2-4 variants, sent as an album)"
        )
        return
    label = "🎨 Generating image…" if count == 1 else f"🎨 Generating {count} images…"
    status = await update.message.reply_text(label)
    try:
        imgs = await _gen_images(prompt, count)
    except Exception as e:
        log.exception("image generation failed")
        try:
            await status.edit_text(f"Image generation failed: {e}")
        except Exception:
            pass
        return
    try:
        await status.delete()
    except Exception:
        pass

    def _as_media(img, i: int):
        caption = prompt[:1000] if i == 0 else None
        if isinstance(img, bytes):
            buf = BytesIO(img)
            buf.name = f"image{i}.png"
            return buf, caption
        return img, caption

    if len(imgs) == 1:
        media, caption = _as_media(imgs[0], 0)
        await update.message.reply_photo(media, caption=caption)
    else:
        group = [
            InputMediaPhoto(media, caption=caption)
            for media, caption in (_as_media(img, i) for i, img in enumerate(imgs))
        ]
        await update.message.reply_media_group(group)


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    try:
        n = max(1, min(int(ctx.args[0]), 400)) if ctx.args else 50
    except ValueError:
        await update.message.reply_text("Usage: /logs [n]  (1-400, default 50)")
        return
    lines = list(_ring.buf)[-n:]
    if not lines:
        await update.message.reply_text("No logs yet.")
        return
    text = "\n".join(lines)
    if len(text) > 3500:
        buf = BytesIO(text.encode())
        buf.name = "bot.log"
        await update.message.reply_document(buf, filename="bot.log")
    else:
        await update.message.reply_text(text)


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text("Unknown command. Use /help.")


async def on_reaction(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """React-to-command: user reactions on the bot's own messages.

    👍 = approve / go ahead, 👎 = not good / reconsider (both are fed back to
    Claude with the reacted text as context); ❤/🔥/💯 = save that message to
    workspace/saved/. Anything else is ignored.
    """
    mr = update.message_reaction
    if mr is None or mr.user is None or mr.user.id != ALLOWED_USER:
        return

    def emojis(reactions) -> set[str]:
        return {
            getattr(r, "emoji", None)
            for r in reactions
            if getattr(r, "emoji", None)
        }

    added = emojis(mr.new_reaction) - emojis(mr.old_reaction)
    if not added:
        return
    text = _SENT_CACHE.get(mr.message_id)
    chat_id = mr.chat.id

    if "👍" in added or "👎" in added:
        if not is_authed():
            await ctx.bot.send_message(
                chat_id=chat_id, text="Claude not authenticated. Use /login."
            )
            return
        excerpt = (text or "(message not cached — too old)")[:500]
        if "👍" in added:
            prompt = (
                "The user reacted 👍 (approve / go ahead) to this message of "
                f"yours:\n\n{excerpt}\n\nProceed accordingly."
            )
        else:
            prompt = (
                "The user reacted 👎 (not good / rejected) to this message of "
                f"yours:\n\n{excerpt}\n\nReconsider and propose an alternative."
            )
        await run_claude_turn(ctx.bot, chat_id, mr.user.id, prompt)
        return

    if added & {"❤", "❤️", "🔥", "💯"}:
        if not text:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text="Can't save: message too old (not in cache anymore).",
            )
            return
        saved_dir = WORKSPACE_DIR / SAVED_DIR_NAME
        saved_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = saved_dir / f"{stamp}.md"
        target.write_text(text + "\n")
        await ctx.bot.send_message(chat_id=chat_id, text=f"💾 Saved to {target}")


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("new", "new conversation"),
        BotCommand("session", "list/switch named sessions"),
        BotCommand("cancel", "kill the current Claude run"),
        BotCommand("compact", "compact context"),
        BotCommand("status", "session, running state, costs"),
        BotCommand("quick", "persistent quick-prompt keyboard"),
        BotCommand("logs", "last bot log lines"),
        BotCommand("img", "generate an image (OpenAI)"),
        BotCommand("export", "download the session transcript"),
        BotCommand("auth", "check auth status"),
        BotCommand("login", "sign in via browser link"),
        BotCommand("file", "retrieve files matching a query"),
        BotCommand("get", "send a workspace file by path"),
        BotCommand("model", "show or set Claude model"),
        BotCommand("schedule", "schedule recurring prompts"),
        BotCommand("help", "list commands"),
    ]
    await app.bot.set_my_commands(commands)


def main() -> None:
    global scheduler

    log.info("Starting tg-claude-runner...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        # Handlers run concurrently so /cancel, /get etc. work while Claude
        # is busy, and new messages get instant "queued" feedback. Actual
        # Claude turns stay serialized by ClaudeRunner's per-uid lock (FIFO).
        .concurrent_updates(True)
        .build()
    )

    async def run_scheduled(
        prompt: str, chat_id: int, uid: int, model: str | None = None
    ) -> None:
        # Isolated one-shot session: recurring jobs must not pollute (or
        # depend on) the interactive chat session's context.
        try:
            resp = await claude.ask_isolated(prompt, model=model)
        except ClaudeSessionError as e:
            await app.bot.send_message(chat_id=chat_id, text=session_error_reply(e))
            return
        except Exception as e:
            await app.bot.send_message(chat_id=chat_id, text=f"Scheduled job error: {e}")
            return
        await send_md(app.bot, chat_id, resp)

    scheduler = Scheduler(JOBS_FILE, app.job_queue, run_scheduled)
    scheduler.load()

    # Claude's proactive-message inbox (see check_notify / _PROACTIVE_PROMPT).
    app.job_queue.run_repeating(check_notify, interval=5, first=10, name="notify-inbox")
    # Claude's self-scheduling inbox (see check_schedule): lets Claude register
    # persistent jobs by dropping a file, without the user typing /schedule.
    app.job_queue.run_repeating(check_schedule, interval=5, first=12, name="schedule-inbox")
    # Pinned status card refresh (only edits when the text changed).
    app.job_queue.run_repeating(update_pinned, interval=30, first=15, name="pinned-status")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CallbackQueryHandler(on_session_callback, pattern=r"^session:"))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("file", cmd_file))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("img", cmd_img))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("quick", cmd_quick))
    app.add_handler(CallbackQueryHandler(on_rerun_callback, pattern=r"^rerun:"))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(MessageReactionHandler(on_reaction))

    # Edited text messages get the re-run offer; registered before handle_text
    # so the generic text handler only sees fresh messages.
    app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & filters.TEXT & ~filters.COMMAND,
        on_edited,
    ))
    app.add_handler(CallbackQueryHandler(on_model_callback, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(on_fget_callback, pattern=r"^fget:"))
    app.add_handler(CallbackQueryHandler(on_tgq_callback, pattern=r"^tgq:"))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    if _openai_client is not None:
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        log.info("Whisper transcription enabled (model=%s)", WHISPER_MODEL)
    else:
        log.info("Whisper transcription disabled (OPENAI_API_KEY not set)")

    upload_filter = (
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION
    )
    app.add_handler(MessageHandler(upload_filter, handle_upload))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Last in the group: only fires for commands no CommandHandler matched.
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))

    # ALL_TYPES so message_reaction updates are delivered (excluded by default).
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
