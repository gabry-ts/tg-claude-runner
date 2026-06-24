#!/usr/bin/env python3
"""Telegram bot wrapping the Claude Code CLI."""

import os
import re
import json
import time
import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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
from .transcript import ToolUse

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tg-claude-runner")

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


class ClaudeRunner:
    """Single persistent Claude Code conversation driven via `claude -p`.

    Single-user bot: one main session for chat, ephemeral sessions for
    isolated lookups (see search()). Lock per uid is kept for API
    compatibility even though we currently only allow ALLOWED_USER.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._main: ClaudeSession | None = None

    def _wanted_model(self) -> str | None:
        m = load_model()
        return None if m == "default" else m

    async def reset(self, uid: int) -> None:
        async with self._locks[uid]:
            if self._main is not None:
                await self._main.reset()

    async def ask(
        self,
        uid: int,
        text: str,
        on_tool_use: ToolUseCallback | None = None,
    ) -> str:
        async with self._locks[uid]:
            wanted = self._wanted_model()
            if self._main is None:
                self._main = ClaudeSession(
                    cwd=WORKSPACE_DIR, model=wanted, persist_state=True,
                )
            elif self._main.model != wanted:
                log.info(
                    "model switched %s -> %s; resetting session",
                    self._main.model, wanted,
                )
                await self._main.reset()
                self._main.set_model(wanted)

            log.info("ask: %s", text[:200])
            try:
                return await self._main.ask(text, on_tool_use=on_tool_use)
            except ClaudeSessionError as e:
                log.error("ClaudeSession error: %s", e)
                return f"Claude session error: {e}"

    async def search(self, prompt: str) -> str:
        """Run an isolated one-shot query in a fresh, non-persisted session.

        Used by /file so the search context never pollutes the main chat
        session: it runs without --resume and never saves a session id.
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
                log.warning("search session cleanup failed: %s", e)


claude = ClaudeRunner()
scheduler: Scheduler | None = None


def is_authed() -> bool:
    """True only if credentials exist AND the OAuth token has not expired.

    A stored token can outlive its ``expiresAt`` with no usable refresh token,
    in which case Claude returns 401 and the session fails. Catching it here
    lets the bot ask the user to /login instead of failing on every message.
    """
    if not CLAUDE_CREDS.exists():
        return False
    try:
        data = json.loads(CLAUDE_CREDS.read_text())
        oauth = data.get("claudeAiOauth") or data
        expires_at = oauth.get("expiresAt")
    except Exception:
        return True  # unknown shape — assume usable, let Claude surface errors
    if not expires_at:
        return True
    return expires_at > time.time() * 1000


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


async def _send_file(update: Update, path: Path, caption: str | None = None) -> None:
    if not path.exists() or not path.is_file():
        await update.message.reply_text(f"Not found: {path}")
        return
    ext = path.suffix.lower()
    try:
        with path.open("rb") as fh:
            if ext in IMAGE_EXTS:
                await update.message.reply_photo(fh, caption=caption)
            else:
                await update.message.reply_document(
                    fh, filename=path.name, caption=caption
                )
    except Exception as e:
        await update.message.reply_text(f"Failed to send {path.name}: {e}")


async def reply_md(update: Update, text: str) -> None:
    if not text:
        text = "(no response)"
    converted = to_telegram_markdown(text)
    for chunk in split_for_telegram(converted):
        try:
            await update.message.reply_text(
                chunk,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("MarkdownV2 send failed (%s); falling back to plain", e)
            for plain in split_for_telegram(text):
                await update.message.reply_text(plain)
            return


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


async def claude_reply(update: Update, prompt: str) -> None:
    """Ask Claude while updating a status message with live tool-use progress.

    First edit fires immediately on the first tool; subsequent edits are
    throttled to one per 1.5s to stay under Telegram's edit_message_text
    rate limit. The status message is deleted once Claude returns and
    the full response is sent via reply_md().
    """
    uid = update.effective_user.id
    status = await update.message.reply_text("🤔 thinking…")
    state = {"last_edit": 0.0}

    async def on_tool(tu: ToolUse) -> None:
        now = time.monotonic()
        if state["last_edit"] != 0.0 and now - state["last_edit"] < 1.5:
            return
        state["last_edit"] = now
        try:
            await status.edit_text(_format_tool(tu))
        except Exception as e:
            log.debug("progress edit failed: %s", e)

    try:
        resp = await claude.ask(uid, prompt, on_tool_use=on_tool)
    except Exception as e:
        log.exception("claude_reply: ask failed")
        try:
            await status.edit_text(f"Error: {e}")
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass
    await reply_md(update, resp)


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
        "/compact - compact context\n"
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


async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await claude_reply(
        update,
        "/compact focus: keep important conversation context",
    )


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

    query = " ".join(ctx.args).strip()
    full_prompt = _FILE_SEARCH_SYSTEM + "\n\nUser request: " + query

    log.info("file-search [isolated] query=%s", query[:200])
    await update.message.reply_chat_action("typing")

    try:
        result_text = await claude.search(full_prompt)
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
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
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


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    voice = update.message.voice or update.message.audio
    if not voice:
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

        await claude_reply(update, transcribed)
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
            await update.message.reply_text("Credentials saved. Claude is ready.")
        except Exception as e:
            await update.message.reply_text(f"Invalid JSON: {e}")
        return

    if not is_authed():
        await update.message.reply_text("Claude not authenticated. Use /login.")
        return

    await claude_reply(update, text)


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("new", "new conversation"),
        BotCommand("compact", "compact context"),
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
        .build()
    )

    async def run_scheduled(prompt: str, chat_id: int, uid: int) -> None:
        try:
            resp = await claude.ask(uid, prompt)
        except Exception as e:
            await app.bot.send_message(chat_id=chat_id, text=f"Scheduled job error: {e}")
            return
        converted = to_telegram_markdown(resp)
        for chunk in split_for_telegram(converted):
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            except Exception:
                for plain in split_for_telegram(resp):
                    await app.bot.send_message(chat_id=chat_id, text=plain)
                return

    scheduler = Scheduler(JOBS_FILE, app.job_queue, run_scheduled)
    scheduler.load()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("file", cmd_file))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_model_callback, pattern=r"^model:"))
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

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
