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

echo "[entrypoint] Starting bot as user node..."
exec gosu node python3 -u -m bot.main
