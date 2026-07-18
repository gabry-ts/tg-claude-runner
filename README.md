# tg-claude-runner

Telegram bot that wraps the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI and runs it inside a Docker container. The container's `/home/node` and `/workspace` are bind-mounted to a single folder on the host (default `~/claude_bot/`), so every CLI credential, key, config, and file Claude touches survives container recreation, image rebuild, or `docker compose down && up`.

## What you get

- **Telegram bridge to Claude Code**: free-text messages are sent to Claude Code in headless print mode (`claude -p --output-format stream-json`). Each turn streams back the assistant text plus live tool-use events; the conversation is continued with `--resume <session_id>`, which also lets sessions survive bot restarts.
- **Named sessions**: `/session work` switches to (or creates) a separate conversation; `/session` lists them with one-tap switch buttons, `/session del <name>` deletes one. Each named session keeps its own `--resume` id, so contexts never bleed into each other; `/new` resets only the current one.
- **Forum topics = sessions**: add the bot to a group with Topics enabled and each topic automatically becomes its own named session — switching conversation is just switching tab, replies stay inside their topic.
- **Proactive messages from Claude**: any file dropped into `workspace/.notify/` is delivered to your chat within seconds and deleted. Claude is told about this in its system prompt, so it can set up background scripts or cron jobs that ping you on their own ("the deploy finished", "disk almost full"). A JSON payload with `question`/`options` becomes an **actionable notification**: you answer with buttons (or a poll) and the answer reaches Claude as a new turn.
- **Reply & quote targeting**: reply (or quote-reply a precise slice) to any earlier message and Claude receives that exact text as explicit context — no copy-pasting to resume an old thread of discussion.
- **Edited message = re-run**: fix a typo in an already-sent prompt and the bot offers a one-tap "🔁 Re-run" with the corrected text.
- **Quick keyboard**: `/quick add <prompt>` builds a persistent reply keyboard with your recurring prompts (12 max); `/quick rm <n>` / `/quick hide` manage it.
- **Pinned live status**: `/status pin` pins a status card that self-updates every 30s (session, model, running state, costs); `/status unpin` removes it.
- **Long-run celebration**: replies to runs longer than 5 minutes arrive with a 🎉 message effect and an enlarged 👍 reaction.
- **Expandable quotes**: markdown blockquotes in replies render as native Telegram quotes, collapsed when longer than 3 lines — long log dumps stop flooding the chat.
- **Message queue**: messages sent while Claude is busy get an instant "📥 Queued" reply and run in order (best-effort FIFO) when the current turn finishes. `/cancel` kills the in-flight run (queued messages still run — repeat `/cancel` to kill the next one); all other commands (`/get`, `/status`, `/logs`, `/auth`, `/login`, `/model`, …) keep working while Claude is busy.
- **Live streaming**: the status message is edited in place with the partial assistant text and the latest tool action as Claude works, so you watch the reply grow instead of staring at "thinking…".
- **Inline questions**: when Claude needs you to pick between options, the choices arrive as Telegram inline buttons — tap one and the answer goes back to Claude as your next turn. Multi-select questions (`"multi": true`) render as a native non-anonymous poll instead. (Under the hood a system prompt teaches Claude to end such replies with a `TGQUESTION: {...}` JSON marker; the bot strips it and renders 2–6 options. Button payloads live in an in-memory cache, so they expire on bot restart.)
- **File buttons**: workspace files mentioned in a reply get "📎" download buttons, no manual `/get` needed.
- **Copy buttons**: short fenced code blocks (≤256 chars, Telegram's cap) in a reply get one-tap "📋" copy buttons under the message — native clipboard copy, no long-press selection.
- **`/status`**: auth state, selected model, active session, current run duration, last-turn cost/duration/tokens, cumulative session cost, bot uptime.
- **`/model`**: switch the Claude model via inline buttons — aliases (`opus`, `sonnet`, `haiku`, `opusplan`) that track the latest version plus pinned IDs (Sonnet 5, Opus 4.8, Haiku 4.5); `/model <name>` accepts any model id as a free-text escape hatch. Persisted to `data/model.json`; switching models resets the session.
- **Transient-error retry**: overloaded/5xx/network errors are retried automatically with backoff before you ever see them.
- **Emoji reactions**: the bot reacts 👀 to your message when it starts working on it, 👍 when done, 💔 on failure — ambient feedback without extra chat noise.
- **Reactions as commands**: react to a bot message with 👍 to tell Claude "go ahead", 👎 for "reconsider" (the reacted text is sent back as context), or ❤/🔥/💯 to save that message to `workspace/saved/` as a note. The bot keeps the text of only the last ~30 sent messages in memory — reacting to anything older (or sent before a restart) gets a "too old" response.
- **Image generation**: `/img [n] <prompt>` generates an image via OpenAI (`gpt-image-1` by default, `IMAGE_MODEL` to change); `n` = 2–4 variants sent together as an album.
- **`/export`**: downloads the current session's transcript as a markdown file (user/assistant turns plus tool-use markers).
- **`/logs [n]`**: last n bot log lines (default 50, max 400) from an in-memory ring buffer; long output is sent as a `bot.log` file.
- **MarkdownV2 rendering**: Claude's markdown output is auto-escaped to Telegram-safe MarkdownV2 (bold, italic, code, links, code blocks).
- **Built-in scheduler**: `/schedule add "<cron>" <prompt>` registers a recurring job, persisted to `data/jobs.json`, restored on restart.
- **Pre-installed CLIs**: `gh`, `git`, `curl`, `wget`, `jq`, `rsync`, `ssh`, `vim`, `nano`, `tree`, `zip/unzip`, plus `node`, `npm`, `python3`, `pip`.
- **Sudo for the bot user**: Claude can `sudo apt-get install …` from a session.
- **Persistent home**: `gh auth login`, `aws configure`, `git config`, ssh keys, `pip --user` installs, `npm install -g`, etc. all live in `~/claude_bot/` on the host and survive any container event.

## Quickstart

```bash
git clone <this-repo> tg-claude-runner
cd tg-claude-runner

cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=...   (from @BotFather)
#   TELEGRAM_ALLOWED_USER=...  (your numeric Telegram id; only you can talk to the bot)

# The host folder Claude lives in. Default ~/claude_bot.
mkdir -p ~/claude_bot/workspace

docker compose up -d --build
docker compose logs -f
```

In Telegram, open the bot and:

1. `/login` → open the link, sign into claude.ai (email + code), paste back the code shown
2. Send any message → it goes to Claude

## How the volumes are laid out

```
HOST                                 CONTAINER
----                                 ---------
~/claude_bot/             <-- bind --> /home/node/      (CLI creds & config)
  .claude/.credentials.json            .claude/.credentials.json
  .config/gh/                          .config/gh/
  .ssh/                                .ssh/
  .gitconfig                           .gitconfig
  .npm-global/                         .npm-global/
  .local/                              .local/

~/claude_bot/workspace/   <-- bind --> /workspace/      (cwd of every Claude call)
  CLAUDE.md                            CLAUDE.md
  .claude/skills/                      .claude/skills/
  scripts/                             scripts/
  init.sh                              init.sh          (optional, see below)

<repo>/data/              <-- bind --> /app/data/       (bot internal state)
  jobs.json                            jobs.json
  model.json                           model.json
```

`CLAUDE_BOT_HOME` in `.env` overrides the default host path (`~/claude_bot`).

## Configuring Claude

Drop anything into `~/claude_bot/workspace/`. Claude will see it because that's its `cwd`:

- `CLAUDE.md` — project-wide instructions
- `.claude/settings.json` — Claude Code settings
- `.claude/skills/` — your custom skills
- `scripts/`, `tools/`, whatever — Claude can read and run them

The bot's own Python code is in the container at `/app/` and is **not** visible to Claude.

## Login flow

**Browser login (default):** `/login` sends you a claude.ai link. Open it, sign in as usual (email + one-time code), approve the authorization, and paste the short code shown back into the chat. The bot exchanges it for tokens (PKCE — it never sees your email/password/code) and writes `/home/node/.claude/.credentials.json` itself. Because this is a fresh OAuth grant, the bot's tokens are independent from your other machines — no refresh-token rotation conflicts. The pasted code message is deleted right away.

This uses the same OAuth endpoints the Claude Code CLI itself talks to (not a separately documented public API), so if it ever breaks there's a fallback:

**Fallback — paste credentials from an authenticated machine:**

**macOS:**
```bash
security find-generic-password -s "Claude Code-credentials" -w
```

**Linux:**
```bash
cat ~/.claude/.credentials.json
```

Paste the JSON to the bot. It's written to the same place, which lives in the bind-mounted `~/claude_bot/` so it persists across everything. The bot deletes your pasted message right after saving, so the OAuth token doesn't linger in the chat history.

The credentials contain a short-lived access token plus a refresh token; the Claude CLI renews the access token automatically, so a single `/login` normally lasts until the refresh token itself is invalidated (e.g. rotated by another machine using the same account). When that happens the bot tells you explicitly to `/login` again.

## Scheduling

Standard 5-field cron (`min hour dom month dow`). Quote the cron expression:

```
/schedule add "0 9 * * *" Send me a morning summary using my notes
/schedule add "*/30 * * * *" Check for new emails
/schedule add "0 8 * * *" model=haiku Quick weather check
/schedule list
/schedule remove abc12345
```

Cron times are evaluated in **Europe/Rome** by default (DST-aware); set `TGCR_TZ` to change the timezone. An optional `model=<name>` right after the cron runs that job on a specific model (e.g. `haiku` for cheap frequent checks) without touching your chat model.

Each fire runs in an **isolated one-shot session** (no `--resume`), so scheduled jobs never pollute your interactive chat context.

## Sending files back to Telegram

Two complementary commands:

- **`/get <path>`** — direct download. Path is relative to `/workspace/` (or absolute under it). Sends the file as-is. No Claude call, instant.

  ```
  /get reports/q1.pdf
  /get screenshots/diagram.png
  ```

- **`/file <query>`** — Claude searches `/workspace/` for files matching your description, in an **isolated session** (no `--resume`, the conversational session is untouched). Replies with structured JSON listing `{title, path}` per file; the bot delivers each one.

  ```
  /file mandami il report di gennaio
  /file find all PDFs from last week
  ```

Path traversal is prevented: any `path` outside `/workspace/` is rejected. Telegram's bot API caps single-file uploads at 50 MB.

## Persistence — what survives what

| What | `docker restart` | `docker compose down && up` | image rebuild |
|---|---|---|---|
| `~/claude_bot/` (host bind) | yes | yes | yes |
| `data/jobs.json` (host bind) | yes | yes | yes |
| `pip install --user` (in `~/.local`) | yes | yes | yes |
| `npm install -g` (in `~/.npm-global`) | yes | yes | yes |
| Binaries dropped in `~/claude_bot/workspace/.bin/` | yes | yes | yes |
| `sudo apt-get install …` (in `/usr`, `/var/lib/dpkg`) | **yes** | **no** | **no** |

For `apt` packages you want to survive recreation, put the install commands in `~/claude_bot/workspace/init.sh` and `chmod +x` it. The entrypoint runs it on every container boot. Example:

```bash
#!/bin/bash
set -e
sudo apt-get update
sudo apt-get install -y postgresql-client redis-tools
```

## Commands

| Command | Description |
|---|---|
| `/start` | greeting |
| `/help` | list commands |
| `/auth` | check Claude auth status |
| `/login` | sign in via browser link (fallback: paste credentials JSON) |
| `/new` | reset the current Claude session |
| `/session [name]` | list named sessions (buttons) / switch or create one |
| `/session del <name>` | delete a named session |
| `/cancel` | kill the current Claude run (queued messages still run) |
| `/status [pin\|unpin]` | auth, model, session, running state, costs; `pin` keeps a self-updating card pinned |
| `/quick [add\|rm\|hide]` | persistent reply keyboard with your recurring prompts |
| `/logs [n]` | last n bot log lines (default 50, max 400) |
| `/img [n] <prompt>` | generate image(s) via OpenAI, n = 2-4 variants as an album (needs `OPENAI_API_KEY`) |
| `/model [name]` | show or set the Claude model (`default`, `sonnet`, `opus`, `haiku`) |
| `/export` | download the session transcript as markdown |
| `/compact` | compact context: summarize the session, then restart it seeded with the summary |
| `/file <query>` | retrieve file(s) matching a description (Claude searches workspace, isolated session) |
| `/get <path>` | send a single workspace file by path (no Claude call) |
| `/schedule add "<cron>" <prompt>` | register recurring prompt |
| `/schedule list` | list jobs |
| `/schedule remove <id>` | remove job |

Free-text messages are forwarded to Claude with the current session.

## File uploads

Send a document, photo, video, or animation to the bot. The file is saved to `/workspace/inbox/<timestamp>_<filename>` and Claude is asked to process it. The Telegram **caption** of the upload becomes the prompt; without a caption Claude just acknowledges the file and waits.

Notes:
- Telegram bot API caps file downloads at **20 MB** — larger uploads are rejected with a message.
- For images, Claude reads them via its Read tool (vision is built into the model).
- The captioned upload is one Telegram message: send file + caption together. Sending text and file as two separate messages is treated as two unrelated turns.

## Voice messages (optional)

If `OPENAI_API_KEY` is set in `.env`, voice and audio messages are transcribed via OpenAI Whisper and forwarded to Claude as if you typed them. The bot replies with the transcription (so you can verify) followed by Claude's answer. Without the key, voice messages are silently ignored.

**Voice in, voice out:** when you send a voice message, the reply is also spoken back as a Telegram voice note via OpenAI TTS (markdown/code is stripped before synthesis). Disable with `TGCR_TTS=0`; tune with `TTS_MODEL` / `TTS_VOICE`.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER` | (required) | Numeric Telegram user id allowed to use the bot |
| `CLAUDE_BOT_HOME` | `${HOME}/claude_bot` | Host folder bind-mounted at `/home/node` |
| `OPENAI_API_KEY` | (unset) | If set, enables Whisper transcription of voice/audio messages |
| `WHISPER_MODEL` | `whisper-1` | Whisper model to use |
| `TGCR_TTS` | `1` | Spoken replies to voice messages (needs `OPENAI_API_KEY`); `0` disables |
| `TTS_MODEL` | `gpt-4o-mini-tts` | OpenAI TTS model |
| `TTS_VOICE` | `alloy` | OpenAI TTS voice |
| `IMAGE_MODEL` | `gpt-image-1` | OpenAI image model for `/img` (e.g. `dall-e-3`) |
| `TGCR_RESPONSE_TIMEOUT` | `0` | Max seconds to wait for a Claude reply per turn. `0` = no timeout: a turn runs until Claude finishes or `/cancel` kills it |
| `TGCR_CLAUDE_BIN` | `claude` | Path to the Claude Code binary |
| `TGCR_STATE_DIR` | `~/.tg-claude-runner` | Where `state.json` (named-session resume ids + current session) is kept |
| `TGCR_TZ` | `Europe/Rome` | Timezone for `/schedule` cron evaluation (falls back to `TZ`, then UTC if invalid) |

## Layout

```
.
├── bot/
│   ├── main.py            # entrypoint: handlers, ClaudeRunner facade
│   ├── claude_session.py  # Claude conversation driven via `claude -p` (stream-json)
│   ├── transcript.py      # pure-function parser for Claude Code stream-json events
│   ├── session_state.py   # persistence of session_id/cwd across restarts
│   ├── markdown_v2.py     # markdown -> Telegram MarkdownV2 conversion
│   └── scheduler.py       # JobQueue persistence (data/jobs.json)
├── tests/                 # pytest suite (conftest.py sets fake env before bot.main import)
├── .github/workflows/
│   ├── docker.yml         # image build
│   └── tests.yml          # runs pytest on push / pull_request
├── data/                  # bot internal state (jobs.json, model.json) — bind-mounted
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh          # pre-trusts /workspace, clears onboarding prompts, runs workspace/init.sh
├── requirements.txt
└── .env.example
```

## How the Claude bridge works

```
Telegram message
      │
      ▼
bot/main.py handler  ──▶  ClaudeRunner.ask(uid, text)
                              │
                              ▼
                       ClaudeSession.ask(text)
                              │
                              │  spawn: claude -p --output-format stream-json --verbose
                              │         --dangerously-skip-permissions [--resume <id>] [--model <m>]
                              │  the prompt is written to the subprocess stdin
                              ▼
                       read stdout stream-json events
                              │  assistant events  ── live tool_use → status edits
                              │  result event      ── final text + session_id
                              ▼
                       session_id saved to state.json  ──▶  Telegram reply
```

Each `/new` drops the saved session id so the next prompt starts a fresh Claude session. `/file` and scheduled jobs run without `--resume` (non-persisted one-shots) so they never pollute the chat session.

## Development & tests

The test suite lives in `tests/` and needs no Telegram token, Claude binary, or network — `tests/conftest.py` injects fake env vars and temp dirs before `bot.main` is imported.

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

CI (`.github/workflows/tests.yml`) runs the same command on every push and pull request.

## Security notes

- The bot only responds to `TELEGRAM_ALLOWED_USER`; messages from anyone else are silently dropped.
- Claude is called with `--dangerously-skip-permissions`, meaning it executes tool calls without per-action prompts. Combined with `sudo NOPASSWD` for `node`, this means: anyone who reaches the bot can run anything inside the container. Treat the container as compromisable; keep `TELEGRAM_ALLOWED_USER` correct.
- `~/claude_bot/` will hold sensitive material (Claude OAuth, ssh keys, `gh` token, etc.). Back it up like any other credentials folder.
