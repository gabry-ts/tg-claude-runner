#!/bin/bash
set -e

HOME_DIR="/home/node"
WORKSPACE_DIR="/workspace"
SKEL_DIR="/opt/skel-node"

# Ensure the bind-mounted dirs are owned by node. On Linux this is required
# (the bind takes the host's uid/gid); on macOS Docker Desktop it's a no-op.
mkdir -p "$HOME_DIR" "$WORKSPACE_DIR" "$WORKSPACE_DIR/.bin"
chown -R node:node "$HOME_DIR" "$WORKSPACE_DIR" /app/data 2>/dev/null || true

# Populate skel files (e.g. .npmrc) when the bind-mounted home is empty
# or missing them. We copy non-clobbering so user changes win.
if [ -d "$SKEL_DIR" ]; then
    for f in "$SKEL_DIR"/.[!.]* "$SKEL_DIR"/*; do
        [ -e "$f" ] || continue
        base="$(basename "$f")"
        target="$HOME_DIR/$base"
        if [ ! -e "$target" ]; then
            cp -a "$f" "$target"
            chown -R node:node "$target"
            echo "[entrypoint] Seeded $target from skel"
        fi
    done
fi

# Run the user-defined boot hook (e.g. apt-get installs that need to be
# re-applied after container recreation). Executed as node with sudo available.
INIT_SCRIPT="$WORKSPACE_DIR/init.sh"
if [ -x "$INIT_SCRIPT" ]; then
    echo "[entrypoint] Running $INIT_SCRIPT"
    gosu node bash "$INIT_SCRIPT" || echo "[entrypoint] init.sh exited non-zero (continuing)"
elif [ -f "$INIT_SCRIPT" ]; then
    echo "[entrypoint] Found $INIT_SCRIPT but it is not executable; skipping"
fi

# Pre-trust the workspace and install SessionStart hook so the bot can
# drive Claude Code interactively without manual prompts.
echo "[entrypoint] Installing Claude Code SessionStart hook + pre-trust..."
gosu node python3 - <<'PYEOF'
import json, os, pathlib
home = pathlib.Path(os.environ["HOME"])

trust_file = home / ".claude.json"
try:
    trust_data = json.loads(trust_file.read_text()) if trust_file.exists() else {}
except json.JSONDecodeError:
    trust_data = {}
trust_data["hasCompletedOnboarding"] = True
trust_data.setdefault("hasSeenTasksHint", True)
trust_data.setdefault("hasSeenStashHint", True)
trust_data.setdefault("hasIdeOnboardingBeenShown", True)
projects = trust_data.setdefault("projects", {})
ws_entry = projects.setdefault("/workspace", {})
ws_entry["hasTrustDialogAccepted"] = True
ws_entry.setdefault("hasCompletedProjectOnboarding", True)
trust_file.write_text(json.dumps(trust_data, indent=2))
print(f"[entrypoint]   pre-trusted /workspace + onboarding flags in {trust_file}")

settings_path = home / ".claude" / "settings.json"
settings_path.parent.mkdir(parents=True, exist_ok=True)
try:
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
except json.JSONDecodeError:
    settings = {}
hooks = settings.setdefault("hooks", {})
session_start = hooks.setdefault("SessionStart", [])
hook_cmd = "python3 /app/bot/hook_runner.py"
hook_matcher = "startup|resume|clear"

# Drop any existing entry that mentions our command (covers older installs
# that lacked a matcher and never fired). Then add a fresh, correct one.
filtered = [
    g for g in session_start
    if not (
        isinstance(g, dict)
        and any(
            isinstance(h, dict) and h.get("command") == hook_cmd
            for h in (g.get("hooks") or [])
        )
    )
]
filtered.append(
    {
        "matcher": hook_matcher,
        "hooks": [{"type": "command", "command": hook_cmd, "timeout": 5}],
    }
)
hooks["SessionStart"] = filtered
settings_path.write_text(json.dumps(settings, indent=2))
print(f"[entrypoint]   SessionStart hook ensured in {settings_path}")
PYEOF

echo "[entrypoint] Starting bot as user node..."
exec gosu node python3 -u -m bot.main
