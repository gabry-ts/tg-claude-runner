#!/usr/bin/env python3
"""PoC for tmux + JSONL technique driving Claude Code in interactive mode.

Validates assumptions for the v2.0 refactor:
  1. SessionStart hook fires and exposes session_id + cwd via stdin JSON
  2. JSONL transcript path follows the encoded-cwd convention
  3. assistant messages carry stop_reason == "end_turn" when complete
  4. tmux send-keys reliably delivers prompts to interactive claude
  5. --dangerously-skip-permissions + IS_SANDBOX=1 prevents y/n prompts

Run: python3 scripts/poc_tmux_jsonl.py
Exit 0 on success, non-zero on any failure. Restores ~/.claude/settings.json
before exiting (best effort).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
PROJECTS = HOME / ".claude" / "projects"

POC_DIR = Path("/tmp/poc-tgcr")
SETTINGS_BACKUP = POC_DIR / "settings.backup.json"
SESSION_MAP = POC_DIR / "session-map.json"
HOOK_SCRIPT = POC_DIR / "hook.py"
PROMPT_FILE = POC_DIR / "README.md"

TMUX_SESSION = "poc-tgcr"
TMUX_WINDOW = f"{TMUX_SESSION}:0"

SESSION_ID_TIMEOUT_S = 30
RESPONSE_TIMEOUT_S = 90
JSONL_POLL_S = 1.0

PROMPT = "Reply with EXACTLY this sentence and nothing else: POC_OK_42"


def log(msg: str) -> None:
    print(f"[poc] {msg}", flush=True)


def tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=check
    )


def kill_session() -> None:
    tmux("kill-session", "-t", TMUX_SESSION)


def write_hook_script() -> None:
    HOOK_SCRIPT.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"out = pathlib.Path({str(SESSION_MAP)!r})\n"
        "try:\n"
        "    data = json.load(sys.stdin)\n"
        "except Exception as e:\n"
        "    data = {'error': str(e)}\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text(json.dumps(data, indent=2))\n"
    )
    HOOK_SCRIPT.chmod(0o755)


def install_hook() -> None:
    POC_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    if SETTINGS.exists():
        SETTINGS_BACKUP.write_text(SETTINGS.read_text())
        settings = json.loads(SETTINGS.read_text())
    else:
        SETTINGS_BACKUP.write_text("__ABSENT__")
        settings = {}

    hooks = settings.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])
    session_start.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 {HOOK_SCRIPT}",
                    "timeout": 5,
                }
            ]
        }
    )
    SETTINGS.write_text(json.dumps(settings, indent=2))


def restore_settings() -> None:
    if not SETTINGS_BACKUP.exists():
        return
    content = SETTINGS_BACKUP.read_text()
    if content == "__ABSENT__":
        SETTINGS.unlink(missing_ok=True)
    else:
        SETTINGS.write_text(content)
    SETTINGS_BACKUP.unlink(missing_ok=True)


def prepare_workspace() -> Path:
    cwd = POC_DIR / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    (cwd / "README.md").write_text("Marker: 42\n")
    return cwd


def start_claude(cwd: Path) -> None:
    kill_session()
    if SESSION_MAP.exists():
        SESSION_MAP.unlink()

    res = tmux(
        "new-session",
        "-d",
        "-s",
        TMUX_SESSION,
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        str(cwd),
        "env",
        "IS_SANDBOX=1",
        "claude",
        "--dangerously-skip-permissions",
    )
    if res.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {res.stderr}")


def wait_for_session_id(timeout: float) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if SESSION_MAP.exists():
            try:
                data = json.loads(SESSION_MAP.read_text())
            except json.JSONDecodeError:
                time.sleep(0.3)
                continue
            sid = data.get("session_id")
            cwd = data.get("cwd")
            if sid and cwd:
                return sid, cwd
        time.sleep(0.3)
    raise TimeoutError("hook did not write session_map within timeout")


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)


def find_jsonl(session_id: str, cwd: str) -> Path:
    return PROJECTS / encode_cwd(cwd) / f"{session_id}.jsonl"


def capture_pane() -> str:
    res = tmux("capture-pane", "-p", "-t", TMUX_WINDOW)
    return res.stdout


def send_prompt(text: str) -> None:
    time.sleep(3)
    log("  pane preview before send:")
    for line in capture_pane().splitlines()[-10:]:
        log(f"    | {line}")
    tmux("send-keys", "-t", TMUX_WINDOW, "-l", text)
    time.sleep(0.5)
    tmux("send-keys", "-t", TMUX_WINDOW, "Enter")


def parse_assistant_text(entry: dict) -> str:
    content = entry.get("message", {}).get("content", []) or []
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text") or ""
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def get_stop_reason(entry: dict) -> str | None:
    return entry.get("message", {}).get("stop_reason")


def tail_jsonl_until_end_turn(path: Path, timeout: float) -> tuple[str, dict]:
    deadline = time.time() + timeout
    entries_seen = 0
    last_assistant_with_text = None
    log(f"  polling {path}")
    while time.time() < deadline:
        if not path.exists():
            time.sleep(JSONL_POLL_S)
            continue
        with path.open("r") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                entries_seen += 1
                etype = entry.get("type")
                if etype == "assistant":
                    sr = get_stop_reason(entry)
                    text = parse_assistant_text(entry)
                    if text:
                        last_assistant_with_text = entry
                    log(f"    [entry {entries_seen}] assistant stop_reason={sr} text_len={len(text)}")
                    if sr == "end_turn":
                        return text, entry
                elif etype == "user":
                    log(f"    [entry {entries_seen}] user")
                else:
                    log(f"    [entry {entries_seen}] type={etype}")
        time.sleep(JSONL_POLL_S)
    if last_assistant_with_text:
        log("  timeout reached but found assistant text without end_turn — dumping last entry:")
        log(json.dumps(last_assistant_with_text, indent=2)[:1500])
    raise TimeoutError(f"no end_turn within {timeout}s ({entries_seen} entries seen)")


def main() -> int:
    log("=== PoC tmux + JSONL Claude Code ===")
    log(f"tmux session: {TMUX_SESSION}")
    log(f"poc dir: {POC_DIR}")
    cwd = prepare_workspace()
    log(f"workspace cwd: {cwd}")

    write_hook_script()
    log("[1] hook script written")

    install_hook()
    log("[2] hook installed in settings.json (with backup)")

    try:
        start_claude(cwd)
        log("[3] tmux session started with claude")

        session_id, hook_cwd = wait_for_session_id(SESSION_ID_TIMEOUT_S)
        log(f"[4] session_id discovered: {session_id}")
        log(f"    cwd from hook:        {hook_cwd}")

        jsonl_path = find_jsonl(session_id, hook_cwd)
        log(f"[5] expected JSONL path: {jsonl_path}")
        log(f"    JSONL exists at boot: {jsonl_path.exists()}")

        log(f"[6] sending prompt: {PROMPT!r}")
        send_prompt(PROMPT)

        text, entry = tail_jsonl_until_end_turn(jsonl_path, RESPONSE_TIMEOUT_S)
        log("[7] response received")
        log(f"--- response text ---\n{text}\n---------------------")

        if "POC_OK_42" in text:
            log("✓ SUCCESS: end_turn detection works, JSONL parsing works, prompt delivered")
            log("VALIDATED ASSUMPTIONS:")
            log("  ✓ SessionStart hook fires and exposes session_id+cwd")
            log("  ✓ JSONL at ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl")
            log("  ✓ assistant entries carry message.stop_reason")
            log("  ✓ tmux send-keys -l + Enter delivers prompt")
            log("  ✓ --dangerously-skip-permissions + IS_SANDBOX=1 lets claude run")
            return 0
        log(f"✗ unexpected response, marker POC_OK_42 not found in: {text!r}")
        return 2
    finally:
        log("[cleanup] killing tmux session...")
        kill_session()
        log("[cleanup] restoring settings.json...")
        restore_settings()
        log("[cleanup] done")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(1)
