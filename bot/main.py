#!/usr/bin/env python3
"""Telegram bot wrapping the Claude Code CLI."""

import os
import re
import json
import asyncio
import logging
import tempfile
from pathlib import Path
from collections import defaultdict

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from .markdown_v2 import to_telegram_markdown, split_for_telegram
from .scheduler import Scheduler

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
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "180"))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

if OPENAI_API_KEY:
    from openai import OpenAI
    _openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    _openai_client = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

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
    """One lock per user; sessions persist via Claude --resume."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sessions: dict[int, str | None] = {}

    def reset(self, uid: int) -> None:
        self._sessions[uid] = None

    async def ask(self, uid: int, text: str) -> str:
        async with self._locks[uid]:
            session_id = self._sessions.get(uid)

            cmd = [
                "claude",
                "-p", text,
                "--output-format", "json",
                "--dangerously-skip-permissions",
            ]
            if session_id:
                cmd += ["--resume", session_id]

            log.info("claude [%s] cwd=%s", session_id or "new", WORKSPACE_DIR)
            log.info("prompt: %s", text[:200])

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(WORKSPACE_DIR),
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=CLAUDE_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                return f"Timeout: Claude exceeded {CLAUDE_TIMEOUT}s."

            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")

            log.info(
                "claude exit=%d stdout=%dB stderr=%dB",
                proc.returncode, len(out), len(err),
            )
            if err:
                log.error("stderr: %s", err[:500])

            if proc.returncode != 0:
                msg = err[:500] or out[:500] or "(no output)"
                return f"Claude error (exit {proc.returncode}): {msg}"

            try:
                data = json.loads(out)
                if "session_id" in data:
                    self._sessions[uid] = data["session_id"]
                return data.get("result", "(empty result)")
            except json.JSONDecodeError:
                return out[:4000] or "(empty response)"


claude = ClaudeRunner()
scheduler: Scheduler | None = None


def is_authed() -> bool:
    return CLAUDE_CREDS.exists()


def save_credentials_json(creds_json: str) -> None:
    json.loads(creds_json)
    CLAUDE_CREDS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_CREDS.write_text(creds_json)
    CLAUDE_CREDS.chmod(0o600)
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


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
        '/schedule add "<cron>" <prompt> - recurring prompt\n'
        "/schedule list - list jobs\n"
        "/schedule remove <id> - remove job\n"
        "/help - this list"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    claude.reset(update.effective_user.id)
    await update.message.reply_text("New session started.")


async def cmd_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_chat_action("typing")
    resp = await claude.ask(
        update.effective_user.id,
        "/compact focus: keep important conversation context",
    )
    await reply_md(update, resp)


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

    cmd = [
        "claude",
        "-p", full_prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]
    log.info("file-search [isolated] query=%s", query[:200])

    await update.message.reply_chat_action("typing")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE_DIR),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await update.message.reply_text(f"Timeout after {CLAUDE_TIMEOUT}s.")
        return

    if proc.returncode != 0:
        msg = stderr.decode(errors="replace")[:500] or "(no output)"
        await update.message.reply_text(f"Claude error (exit {proc.returncode}): {msg}")
        return

    try:
        wrapper = json.loads(stdout.decode(errors="replace"))
        result_text = wrapper.get("result", "")
    except json.JSONDecodeError:
        await update.message.reply_text("Could not parse Claude wrapper JSON.")
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

        await update.message.reply_chat_action("typing")
        resp = await claude.ask(update.effective_user.id, transcribed)
        await reply_md(update, resp)
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

    await update.message.reply_chat_action("typing")
    resp = await claude.ask(update.effective_user.id, text)
    await reply_md(update, resp)


async def post_init(app: Application) -> None:
    commands = [
        BotCommand("new", "new conversation"),
        BotCommand("compact", "compact context"),
        BotCommand("auth", "check auth status"),
        BotCommand("login", "paste credentials"),
        BotCommand("file", "retrieve files matching a query"),
        BotCommand("get", "send a workspace file by path"),
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
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    if _openai_client is not None:
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        log.info("Whisper transcription enabled (model=%s)", WHISPER_MODEL)
    else:
        log.info("Whisper transcription disabled (OPENAI_API_KEY not set)")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
