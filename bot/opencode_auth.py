"""Generic provider onboarding for the OpenCode backend.

OpenCode is powered by the models.dev provider list, so *any* provider can be
used by storing an API key in its credentials file. That file is a flat object
mapping a provider id to a credential entry; for API-key providers the entry is
``{"type": "api", "key": "<token>"}`` (the same shape ``opencode auth login``
writes). We support the generic API-key path so the Telegram onboarding can ask
"which provider/plan do you have?" and "paste the token", for any provider.

File location: ``$XDG_DATA_HOME/opencode/auth.json`` (default
``~/.local/share/opencode/auth.json``), which lives in the bind-mounted home so
it survives container recreation like every other credential.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

# A short curated shortlist for the onboarding buttons. NOT exhaustive: the
# "Altro" path accepts any models.dev provider id, so this is just convenience.
COMMON_PROVIDERS: list[tuple[str, str]] = [
    ("OpenCode Zen", "opencode"),
    ("Anthropic", "anthropic"),
    ("OpenAI", "openai"),
    ("OpenRouter", "openrouter"),
    ("Google", "google"),
    ("Groq", "groq"),
    ("xAI", "xai"),
    ("DeepSeek", "deepseek"),
]

_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.IGNORECASE)


def _data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def auth_file() -> Path:
    return _data_dir() / "auth.json"


def load_auth() -> dict:
    try:
        data = json.loads(auth_file().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def providers() -> list[str]:
    """Provider ids that currently have a stored credential."""
    return sorted(load_auth().keys())


def valid_provider(pid: str) -> bool:
    return bool(_PROVIDER_RE.fullmatch(pid.strip()))


def save_api_key(provider: str, key: str) -> None:
    provider = provider.strip().rstrip("/").lower()
    if not valid_provider(provider):
        raise ValueError("provider id non valido")
    key = key.strip()
    if not key:
        raise ValueError("token vuoto")
    auth = load_auth()
    auth[provider] = {"type": "api", "key": key}
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(auth, indent=2))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    path.chmod(0o600)


def _env_has_api_key() -> bool:
    return any(
        k.endswith("_API_KEY") and os.environ.get(k) for k in os.environ.keys()
    )


def is_authed() -> bool:
    """OpenCode can run if at least one provider credential is stored, or if a
    provider key is present in the environment (opencode reads those too)."""
    return bool(providers()) or _env_has_api_key()
