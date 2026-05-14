# tg-claude-runner

Telegram bot that wraps the [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI and runs it inside a Docker container. The container's `/home/node` and `/workspace` are bind-mounted to a single folder on the host (default `~/claude_bot/`), so every CLI credential, key, config, and file Claude touches survives container recreation, image rebuild, or `docker compose down && up`.

## What you get

- **Telegram bridge to Claude Code**: free-text messages are driven into a persistent **interactive** Claude Code session running inside a tmux window. Responses are read from Claude's JSONL transcript (`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`) and streamed back to Telegram. Sessions resume across bot restarts via `--resume <session_id>`. Interactive mode keeps usage on your Claude plan instead of the Agent SDK credit.
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

1. `/login` → follow the prompt and paste your Claude Code credentials JSON
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

The bot doesn't run an OAuth flow — it expects you to extract credentials from a machine where you're already authenticated.

**macOS:**
```bash
security find-generic-password -s "Claude Code-credentials" -w
```

**Linux:**
```bash
cat ~/.claude/.credentials.json
```

Paste the JSON to the bot. It's written to `/home/node/.claude/.credentials.json`, which lives in the bind-mounted `~/claude_bot/` so it persists across everything.

## Scheduling

Standard 5-field cron (`min hour dom month dow`). Quote the cron expression:

```
/schedule add "0 9 * * *" Send me a morning summary using my notes
/schedule add "*/30 * * * *" Check for new emails
/schedule list
/schedule remove abc12345
```

Each fire reuses the most recent Claude session for that user. Use `/new` to start fresh.

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
| `/login` | paste Claude credentials |
| `/new` | reset Claude session |
| `/compact` | compact Claude conversation context |
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

## Configuration

| Env var | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USER` | (required) | Numeric Telegram user id allowed to use the bot |
| `CLAUDE_BOT_HOME` | `${HOME}/claude_bot` | Host folder bind-mounted at `/home/node` |
| `OPENAI_API_KEY` | (unset) | If set, enables Whisper transcription of voice/audio messages |
| `WHISPER_MODEL` | `whisper-1` | Whisper model to use |
| `TGCR_RESPONSE_TIMEOUT` | `300` | Max seconds to wait for a Claude reply (per turn) |
| `TGCR_SESSION_ID_TIMEOUT` | `30` | Max seconds to wait for the SessionStart hook |
| `TGCR_JSONL_POLL` | `1.0` | Interval (seconds) between JSONL polls while waiting for a reply |
| `TGCR_BOOT_GRACE` | `3.0` | Seconds to wait after spawning claude before sending the first prompt |

## Layout

```
.
├── bot/
│   ├── main.py            # entrypoint: handlers, ClaudeRunner facade
│   ├── claude_session.py  # interactive Claude window driven via libtmux + JSONL tail
│   ├── transcript.py      # pure-function parser for Claude Code JSONL events
│   ├── session_state.py   # persistence of session_id/cwd across restarts
│   ├── hook_runner.py     # SessionStart hook (Claude invokes this directly)
│   ├── markdown_v2.py     # markdown -> Telegram MarkdownV2 conversion
│   └── scheduler.py       # JobQueue persistence (data/jobs.json)
├── scripts/
│   └── poc_tmux_jsonl.py  # standalone PoC for the tmux+JSONL technique (debug)
├── data/                  # bot internal state (jobs.json) — bind-mounted
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh          # installs SessionStart hook, pre-trusts /workspace
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
                       ClaudeSession.ensure_started()
                              │
                              │  libtmux: new window "claude" running
                              │     env IS_SANDBOX=1 claude --dangerously-skip-permissions [--resume <id>]
                              ▼
                       SessionStart hook (bot/hook_runner.py)
                              │  writes session_map.json with session_id + cwd
                              ▼
                       ClaudeSession.send_prompt(text)        ── tmux send-keys -l + Enter
                       ClaudeSession.wait_for_response()      ── aiofiles tail of JSONL transcript
                              │                                  until message.stop_reason == "end_turn"
                              ▼
                       extracted text  ──▶  Telegram reply
```

Each `/new` kills the current tmux window and lets the next prompt spawn a fresh Claude session. `/file` uses a separate ephemeral window (`claude-search`) so its lookup never pollutes the chat session.

## Security notes

- The bot only responds to `TELEGRAM_ALLOWED_USER`; messages from anyone else are silently dropped.
- Claude is called with `--dangerously-skip-permissions`, meaning it executes tool calls without per-action prompts. Combined with `sudo NOPASSWD` for `node`, this means: anyone who reaches the bot can run anything inside the container. Treat the container as compromisable; keep `TELEGRAM_ALLOWED_USER` correct.
- `~/claude_bot/` will hold sensitive material (Claude OAuth, ssh keys, `gh` token, etc.). Back it up like any other credentials folder.
