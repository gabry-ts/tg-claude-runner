"""Browser OAuth login for Claude — the same PKCE flow the Claude Code CLI
uses for `/login`.

The bot never sees the user's email, password or one-time code: the user
signs in on claude.ai in their own browser and pastes back only the
short-lived authorization code shown at the end, which is exchanged here
for an access+refresh token pair and stored in the same .credentials.json
format the CLI writes. Because this is a fresh OAuth grant, the tokens are
independent from any other machine's (no refresh-token rotation conflicts).

Note: these endpoints are what the CLI itself talks to, not a separately
documented public API — the JSON-paste login stays available as fallback.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from urllib.parse import quote

import httpx

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public client id
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "org:create_api_key user:profile user:inference"

LOGIN_TTL_S = 900  # a pending login expires after 15 minutes

_CODE_RE = re.compile(r"[A-Za-z0-9_\-]{8,}#[A-Za-z0-9_\-]{8,}")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def new_login() -> dict:
    """Fresh PKCE state + the claude.ai URL the user must open."""
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(os.urandom(32))
    url = (
        f"{AUTHORIZE_URL}?code=true"
        f"&client_id={CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        f"&scope={quote(SCOPES)}"
        f"&code_challenge={challenge}"
        "&code_challenge_method=S256"
        f"&state={state}"
    )
    return {"url": url, "verifier": verifier, "state": state, "created": time.time()}


def is_expired(login: dict | None) -> bool:
    return login is None or time.time() - login.get("created", 0) > LOGIN_TTL_S


def looks_like_code(text: str) -> bool:
    """The claude.ai code page shows '<code>#<state>'."""
    return bool(_CODE_RE.fullmatch(text.strip()))


async def exchange_code(pasted: str, login: dict) -> dict:
    """Exchange the pasted authorization code for CLI-format credentials."""
    code, _, state = pasted.strip().partition("#")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "state": state or login["state"],
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": login["verifier"],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    oauth_blob = {
        "accessToken": data["access_token"],
        "refreshToken": data.get("refresh_token"),
        "expiresAt": int((time.time() + data.get("expires_in", 3600)) * 1000),
        "scopes": data.get("scope", SCOPES).split(),
    }
    subscription = data.get("subscription_type") or (
        data.get("account") or {}
    ).get("subscription_type")
    if subscription:
        oauth_blob["subscriptionType"] = subscription
    return {"claudeAiOauth": oauth_blob}
