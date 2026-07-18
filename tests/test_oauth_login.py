"""Tests for the browser OAuth login flow (bot/oauth.py + /login wiring)."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from bot import main as m
from bot import oauth


# ---------------------------------------------------------------------------
# oauth primitives
# ---------------------------------------------------------------------------

class TestNewLogin:
    def test_url_carries_pkce_and_state(self):
        login = oauth.new_login()
        parsed = urlparse(login["url"])
        assert parsed.hostname == "claude.ai"
        q = parse_qs(parsed.query)
        assert q["client_id"] == [oauth.CLIENT_ID]
        assert q["response_type"] == ["code"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["state"] == [login["state"]]
        # challenge must be derived from the verifier
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(login["verifier"].encode()).digest()
        ).decode().rstrip("=")
        assert q["code_challenge"] == [expected]
        assert q["redirect_uri"] == [oauth.REDIRECT_URI]

    def test_each_login_is_unique(self):
        a, b = oauth.new_login(), oauth.new_login()
        assert a["verifier"] != b["verifier"]
        assert a["state"] != b["state"]

    def test_expiry(self):
        login = oauth.new_login()
        assert not oauth.is_expired(login)
        login["created"] = time.time() - oauth.LOGIN_TTL_S - 1
        assert oauth.is_expired(login)
        assert oauth.is_expired(None)


class TestLooksLikeCode:
    def test_valid_code(self):
        assert oauth.looks_like_code("AbC123xyz-_#Zz99887766")

    @pytest.mark.parametrize("bad", [
        "just a normal message",
        "short#a",
        '{"claudeAiOauth": {}}',
        "hello#world with spaces",
        "manca-il-cancelletto",
    ])
    def test_invalid(self, bad):
        assert not oauth.looks_like_code(bad)


class TestExchangeCode:
    def _fake_httpx(self, monkeypatch, response_json, status=200):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

            def json(self):
                return response_json

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                captured["url"] = url
                captured["payload"] = json
                return FakeResp()

        monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeClient)
        return captured

    def test_exchange_builds_cli_credentials(self, monkeypatch):
        captured = self._fake_httpx(monkeypatch, {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "user:inference user:profile",
            "account": {"subscription_type": "max"},
        })
        login = oauth.new_login()
        creds = asyncio.run(oauth.exchange_code("thecode123#thestate456", login))
        assert captured["url"] == oauth.TOKEN_URL
        p = captured["payload"]
        assert p["grant_type"] == "authorization_code"
        assert p["code"] == "thecode123"
        assert p["state"] == "thestate456"
        assert p["code_verifier"] == login["verifier"]
        blob = creds["claudeAiOauth"]
        assert blob["accessToken"] == "at-1"
        assert blob["refreshToken"] == "rt-1"
        assert blob["expiresAt"] > time.time() * 1000
        assert blob["subscriptionType"] == "max"
        assert blob["scopes"] == ["user:inference", "user:profile"]

    def test_code_without_state_uses_login_state(self, monkeypatch):
        captured = self._fake_httpx(monkeypatch, {"access_token": "a"})
        login = oauth.new_login()
        # regex requires '#', but exchange itself tolerates a bare code
        asyncio.run(oauth.exchange_code("onlycode1234", login))
        assert captured["payload"]["state"] == login["state"]

    def test_http_error_raises(self, monkeypatch):
        self._fake_httpx(monkeypatch, {}, status=400)
        with pytest.raises(RuntimeError):
            asyncio.run(oauth.exchange_code("a1b2c3d4e5#f6g7h8i9j0", oauth.new_login()))


# ---------------------------------------------------------------------------
# /login wiring in handle_text
# ---------------------------------------------------------------------------

class FakeChat:
    def __init__(self):
        self.id = 10
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append(text)


class FakeMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=1)

    async def delete(self):
        self.deleted = True


def make_update(text, uid=1):
    return SimpleNamespace(
        message=FakeMsg(text),
        effective_message=None,
        effective_user=SimpleNamespace(id=uid),
        effective_chat=FakeChat(),
    )


class TestLoginWiring:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "CLAUDE_CREDS", tmp_path / ".credentials.json")
        monkeypatch.setattr(m, "CHAT_FILE", tmp_path / "chat.json")
        m._PENDING_LOGIN["login"] = None
        yield
        m._PENDING_LOGIN["login"] = None

    def test_cmd_login_sets_pending_and_sends_link(self):
        upd = make_update("/login")

        class Ctx:
            args = []

        asyncio.run(m.cmd_login(upd, Ctx()))
        assert m._PENDING_LOGIN["login"] is not None
        assert "browser" in upd.message.replies[0].lower()

    def test_pasted_code_exchanged_and_saved(self, monkeypatch):
        m._PENDING_LOGIN["login"] = oauth.new_login()

        async def fake_exchange(pasted, login):
            return {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                      "expiresAt": 9999999999999}}

        monkeypatch.setattr(m.oauth, "exchange_code", fake_exchange)
        upd = make_update("a1b2c3d4e5#f6g7h8i9j0")
        asyncio.run(m.handle_text(upd, SimpleNamespace(bot=None, args=[])))
        assert m._PENDING_LOGIN["login"] is None
        assert upd.message.deleted
        assert "Logged in" in upd.effective_chat.sent[0]
        saved = json.loads(m.CLAUDE_CREDS.read_text())
        assert saved["claudeAiOauth"]["accessToken"] == "a"

    def test_expired_login_rejected(self):
        login = oauth.new_login()
        login["created"] = time.time() - oauth.LOGIN_TTL_S - 1
        m._PENDING_LOGIN["login"] = login
        upd = make_update("a1b2c3d4e5#f6g7h8i9j0")
        asyncio.run(m.handle_text(upd, SimpleNamespace(bot=None, args=[])))
        assert "expired" in upd.message.replies[0].lower()
        assert m._PENDING_LOGIN["login"] is None
        assert not m.CLAUDE_CREDS.exists()

    def test_failed_exchange_reports_error(self, monkeypatch):
        m._PENDING_LOGIN["login"] = oauth.new_login()

        async def boom(pasted, login):
            raise RuntimeError("HTTP 400")

        monkeypatch.setattr(m.oauth, "exchange_code", boom)
        upd = make_update("a1b2c3d4e5#f6g7h8i9j0")
        asyncio.run(m.handle_text(upd, SimpleNamespace(bot=None, args=[])))
        assert "Login failed" in upd.message.replies[0]
        # pending stays so the user can retry with a fresh code
        assert m._PENDING_LOGIN["login"] is not None

    def test_code_like_text_without_pending_goes_to_claude_path(self, monkeypatch):
        # no pending login: the message is treated as a normal prompt
        # (unauthenticated -> login hint), not swallowed by the oauth path
        upd = make_update("a1b2c3d4e5#f6g7h8i9j0")
        asyncio.run(m.handle_text(upd, SimpleNamespace(bot=None, args=[])))
        assert any("login" in r.lower() for r in upd.message.replies)
        assert not upd.message.deleted
