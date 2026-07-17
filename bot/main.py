#!/usr/bin/env python3
"""Telegram bot wrapping the Claude Code CLI."""

import os
import re
import json
import time
import uuid
import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from .markdown_v2 import to_telegram_markdown, split_for_telegram
from .scheduler import Scheduler
from .claude_session import ClaudeSession, ClaudeSessionError, ToolUseCallback
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
MODEL_CHOICES = ["default", "sonnet", "opus", "haiku"]

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
TTS_ENABLED = bool(OPENAI_API_KEY) and os.environ.get("TGCR_TTS", "1") != "0"
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")

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
    "genuinely needed. Open-ended questions stay normal text."
)


class ClaudeRunner:
    """Single persistent Claude Code conversation driven via `claude -p`.

    Single-user bot: one main session for chat, ephemeral sessions for
    isolated lookups (see ask_isolated()). Lock per uid is kept for API
    compatibility even though we currently only allow ALLOWED_USER.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._main: ClaudeSession | None = None
        self._busy_since: float | None = None

    def _wanted_model(self) -> str | None:
        m = load_model()
        return None if m == "default" else m

    def busy(self, uid: int) -> bool:
        return self._locks[uid].locked()

    def cancel(self, uid: int) -> bool:
        """Kill the in-flight run of the main session, if any.

        Deliberately does NOT take the lock — the whole point is to interrupt
        the ask() currently holding it. Queued asks behind it still run.
        """
        if self._main is None:
            return False
        return self._main.cancel_current()

    async def reset(self, uid: int) -> None:
        async with self._locks[uid]:
            if self._main is not None:
                await self._main.reset()

    async def ask(
        self,
        uid: int,
        text: str,
        on_tool_use: ToolUseCallback | None = None,
        on_text=None,
    ) -> str:
        async with self._locks[uid]:
            self._busy_since = time.time()
            try:
                wanted = self._wanted_model()
                if self._main is None:
                    self._main = ClaudeSession(
                        cwd=WORKSPACE_DIR,
                        model=wanted,
                        persist_state=True,
                        append_system_prompt=_TGQ_SYSTEM_PROMPT,
                    )
                elif self._main.model != wanted:
                    log.info(
                        "model switched %s -> %s; resetting session",
                        self._main.model, wanted,
                    )
                    await self._main.reset()
                    self._main.set_model(wanted)

                log.info("ask: %s", text[:200])
                return await self._main.ask(
                    text, on_tool_use=on_tool_use, on_text=on_text
                )
            finally:
                self._busy_since = None

    def status_info(self) -> dict:
        s = self._main
        return {
            "session_id": s.session_id if s else None,
            "busy_since": self._busy_since,
            "last_result": s.last_result if s else None,
            "total_cost_usd": s.total_cost_usd if s else 0.0,
        }

    async def ask_isolated(self, prompt: str) -> str:
        """Run an isolated one-shot query in a fresh, non-persisted session.

        Used by /file and scheduled jobs so their context never pollutes the
        main chat session: runs without --resume and never saves a session id.
        """
        wanted = self._wanted_model()
        sess = ClaudeSession(
            cwd=WORKSPACE_DIR,
            model=wanted,
            persist_state=False,
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
    return stripped, {"question": question, "options": options}


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


async def send_md(bot, chat_id: int, text: str) -> None:
    """Send text as MarkdownV2, falling back to plain per chunk.

    The original text is split first and each chunk converted independently,
    so a chunk whose conversion Telegram rejects falls back to plain text on
    its own — earlier chunks are never re-sent. The split margin (3000 vs
    Telegram's 4096) leaves room for escape backslashes added by conversion;
    a chunk that still overflows after conversion is sent plain.
    """
    if not text:
        text = "(no response)"
    for chunk in split_for_telegram(text, max_len=3000):
        converted = to_telegram_markdown(chunk)
        if len(converted) <= 4000:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=converted,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
                continue
            except Exception as e:
                log.warning("MarkdownV2 send failed (%s); falling back to plain", e)
        await bot.send_message(chat_id=chat_id, text=chunk)


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


async def _react(bot, chat_id: int, message_id: int | None, emoji: str | None) -> None:
    """Best-effort reaction on the user's message (👀 working, 👍 done, 💔 error)."""
    if message_id is None:
        return
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)] if emoji else [],
        )
    except Exception as e:
        log.debug("reaction failed: %s", e)


async def run_claude_turn(
    bot, chat_id: int, uid: int, prompt: str, react_to: int | None = None
) -> str | None:
    """Run one Claude turn, streaming progress into a status message.

    The status message shows the partial assistant text as it arrives (tail,
    plain text) and the latest tool-use label; edits are throttled to one per
    1.5s to stay under Telegram's edit rate limit. When Claude returns, the
    status is deleted and the full reply is sent via send_md(), followed by
    download buttons for any workspace files it mentions and inline-keyboard
    rendering of a trailing TGQUESTION marker.
    """
    await _react(bot, chat_id, react_to, "👀")
    if claude.busy(uid):
        status = await bot.send_message(
            chat_id=chat_id,
            text="📥 Queued — Claude is still working on the previous message…",
        )
    else:
        status = await bot.send_message(chat_id=chat_id, text="🤔 thinking…")
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

    try:
        resp = await claude.ask(uid, prompt, on_tool_use=on_tool, on_text=on_text)
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

    resp, tgq = _extract_tgquestion(resp)
    if resp or not tgq:
        await send_md(bot, chat_id, resp)

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
        )

    if tgq:
        token = _cb_store((tgq["question"], tgq["options"], chat_id, uid))
        keyboard = [
            [InlineKeyboardButton(opt, callback_data=f"tgq:{token}:{i}")]
            for i, opt in enumerate(tgq["options"])
        ]
        await bot.send_message(
            chat_id=chat_id,
            text="❓ " + tgq["question"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    await _react(bot, chat_id, react_to, "👍")
    return resp


async def claude_reply(update: Update, prompt: str) -> str | None:
    return await run_claude_turn(
        update.get_bot(),
        update.effective_chat.id,
        update.effective_user.id,
        prompt,
        react_to=update.message.message_id if update.message else None,
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "Bot ready. Send a message to talk to Claude Code.\nUse /help for commands."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "/new - new conversation\n"
        "/cancel - kill the current Claude run\n"
        "/compact - compact context\n"
        "/status - session, running state, costs\n"
        "/logs [n] - last n bot log lines\n"
        "/auth - check Claude auth status\n"
        "/login - paste Claude credentials\n"
        "/file <query> - retrieve files matching a query (Claude searches)\n"
        "/get <path> - send a workspace file by path (no Claude)\n"
        "/model [name] - show or set Claude model\n"
        '/schedule add "<cron>" <prompt> - recurring prompt\n'
        "/schedule list - list jobs\n"
        "/schedule remove <id> - remove job\n"
        "/help - this list"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await claude.reset(update.effective_user.id)
    await update.message.reply_text("New session started.")


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


BOT_STARTED = time.time()


def _fmt_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    info = claude.status_info()
    lines = [
        f"Auth: {'✅' if is_authed() else '❌ — use /login'}",
        f"Model: {load_model()}",
        f"Session: {info['session_id'] or 'none'}",
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
    await update.message.reply_text("\n".join(lines))


async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if is_authed():
        await update.message.reply_text("Claude is authenticated. Use /login to re-auth.")
    else:
        await update.message.reply_text("Claude is not authenticated. Use /login.")


async def cmd_login(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "On a machine where Claude Code is authenticated, run:\n\n"
        "macOS:\n"
        '`security find-generic-password -s "Claude Code-credentials" -w`\n\n'
        "Linux:\n"
        "`cat ~/.claude/.credentials.json`\n\n"
        "Then paste the resulting JSON here.",
        parse_mode="Markdown",
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    if ctx.args:
        name = ctx.args[0].strip()
        if name not in MODEL_CHOICES:
            await update.message.reply_text(
                f"Unknown model. Choices: {', '.join(MODEL_CHOICES)}"
            )
            return
        save_model(name)
        await update.message.reply_text(f"Model set to {name}.")
        return
    current = load_model()
    keyboard = [
        [InlineKeyboardButton(
            f"{'> ' if c == current else ''}{c}",
            callback_data=f"model:{c}",
        )]
        for c in MODEL_CHOICES
    ]
    await update.message.reply_text(
        f"Current: {current}\nChoose a model:",
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
    question, options, chat_id, uid = payload
    if not 0 <= idx < len(options):
        await query.answer("Bad option.")
        return
    choice = options[idx]
    await query.answer()
    try:
        await query.edit_message_text(f"❓ {question}\n➡️ {choice}")
    except Exception:
        pass
    await run_claude_turn(ctx.bot, chat_id, uid, choice)


async def on_model_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith("model:"):
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER:
        await query.answer("Not authorized.")
        return
    name = query.data.split(":", 1)[1]
    if name not in MODEL_CHOICES:
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
        lines = [f"{j['id']}: {j['cron']!r} -> {j['prompt'][:80]}" for j in jobs]
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
        if not prompt:
            await update.message.reply_text("Empty prompt.")
            return
        try:
            job_id = scheduler.add(
                cron=cron_expr,
                prompt=prompt,
                chat_id=update.effective_chat.id,
                uid=update.effective_user.id,
            )
        except ValueError as e:
            await update.message.reply_text(f"Invalid cron: {e}")
            return
        await update.message.reply_text(f"Scheduled job {job_id}.")
        return

    await update.message.reply_text(f"Unknown subcommand: {sub}")


def _safe_filename(name: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


async def handle_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
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


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    text = update.message.text.strip()

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

    await claude_reply(update, text)


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
        from io import BytesIO
        buf = BytesIO(text.encode())
        buf.name = "bot.log"
        await update.message.reply_document(buf, filename="bot.log")
    else:
        await update.message.reply_text(text)


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text("Unknown command. Use /help.")


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("new", "new conversation"),
        BotCommand("cancel", "kill the current Claude run"),
        BotCommand("compact", "compact context"),
        BotCommand("status", "session, running state, costs"),
        BotCommand("logs", "last bot log lines"),
        BotCommand("auth", "check auth status"),
        BotCommand("login", "paste credentials"),
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

    async def run_scheduled(prompt: str, chat_id: int, uid: int) -> None:
        # Isolated one-shot session: recurring jobs must not pollute (or
        # depend on) the interactive chat session's context.
        try:
            resp = await claude.ask_isolated(prompt)
        except ClaudeSessionError as e:
            await app.bot.send_message(chat_id=chat_id, text=session_error_reply(e))
            return
        except Exception as e:
            await app.bot.send_message(chat_id=chat_id, text=f"Scheduled job error: {e}")
            return
        await send_md(app.bot, chat_id, resp)

    scheduler = Scheduler(JOBS_FILE, app.job_queue, run_scheduled)
    scheduler.load()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("file", cmd_file))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
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

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
