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

# Pre-trust the workspace and clear first-run prompts so `claude -p` runs
# non-interactively. Also strip the legacy SessionStart hook left behind by
# older tmux-based builds (the bot no longer drives Claude via tmux).
echo "[entrypoint] Pre-trusting workspace + clearing onboarding prompts..."
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
trust_data["bypassPermissionsModeAccepted"] = True
trust_data["hasAcceptedBypassPermissionsModeWarning"] = True
workspace = os.environ.get("WORKSPACE_DIR", "/workspace")
projects = trust_data.setdefault("projects", {})
ws_entry = projects.setdefault(workspace, {})
ws_entry["hasTrustDialogAccepted"] = True
ws_entry.setdefault("hasCompletedProjectOnboarding", True)
trust_file.write_text(json.dumps(trust_data, indent=2))
print(f"[entrypoint]   pre-trusted {workspace} + onboarding/bypass flags in {trust_file}")

# Migration: remove the legacy SessionStart hook (hook_runner.py) that older
# tmux-based installs persisted into settings.json on the bind-mounted home.
settings_path = home / ".claude" / "settings.json"
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        settings = {}
    hooks = settings.get("hooks") or {}
    session_start = hooks.get("SessionStart") or []
    kept = [
        g for g in session_start
        if not (
            isinstance(g, dict)
            and any(
                isinstance(h, dict) and "hook_runner.py" in (h.get("command") or "")
                for h in (g.get("hooks") or [])
            )
        )
    ]
    if kept != session_start:
        if kept:
            hooks["SessionStart"] = kept
        else:
            hooks.pop("SessionStart", None)
        if hooks:
            settings["hooks"] = hooks
        else:
            settings.pop("hooks", None)
        settings_path.write_text(json.dumps(settings, indent=2))
        print(f"[entrypoint]   removed legacy SessionStart hook from {settings_path}")
PYEOF

echo "[entrypoint] Starting bot as user node..."
exec gosu node python3 -u -m bot.main
