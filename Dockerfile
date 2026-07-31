FROM node:20-slim

# Base system tooling.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    curl wget jq gosu git ca-certificates sudo tzdata \
    openssh-client rsync zip unzip tree less vim nano \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh) — installed from upstream repo
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# OpenCode CLI — the alternative backend (TGCR_BACKEND=opencode). Its
# credentials live in /home/node/.local/share/opencode/ (bind-mounted home), so
# `/login` onboarding survives container recreation like the Claude ones.
RUN npm install -g opencode-ai

# Browser control: a headless Chromium driven by the Playwright MCP server, so
# either backend can navigate/click/type on the web. Browsers are installed into
# a world-readable path (NOT the bind-mounted home) so they survive rebuilds and
# are usable by the `node` user; the MCP server is installed globally so it
# starts without a runtime download. The persistent browser profile lives under
# the bind-mounted home (set in entrypoint), so logins survive container events.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npm install -g @playwright/mcp@latest \
    && npx -y playwright@latest install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright

# Allow `node` to run sudo without a password so Claude can `sudo apt-get install …`
# at runtime. apt-installed packages survive `docker restart` but are wiped on
# `docker compose down && up` or image rebuild — use /workspace/init.sh for
# repeatable installs across container recreations.
RUN echo 'node ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/node \
    && chmod 0440 /etc/sudoers.d/node

# Skel files for /home/node — copied at entrypoint if missing. Required because
# /home/node is bind-mounted from the host and the host folder may be empty on
# first run, hiding anything we'd write here directly.
RUN mkdir -p /opt/skel-node \
    && echo 'prefix=/home/node/.npm-global' > /opt/skel-node/.npmrc \
    && chown -R node:node /opt/skel-node

# Surface user-installed binaries on PATH.
ENV PATH="/home/node/.local/bin:/home/node/.npm-global/bin:/workspace/.bin:${PATH}"

WORKDIR /app

COPY requirements.txt ./
RUN pip3 install --break-system-packages -r requirements.txt

COPY --chown=node:node bot/ ./bot/

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
