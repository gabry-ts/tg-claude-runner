"""Tests for bot/main.py async handlers with hand-rolled Telegram fakes.

All async handlers are invoked via asyncio.run() from plain sync tests.
No pytest-asyncio needed. Fakes record every bot interaction so tests can
assert exact call sequences.
"""

import asyncio
import base64
import itertools
import json
from collections import deque
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import main
from bot.claude_session import ClaudeSessionError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_ids = itertools.count(1000)


class FakeMessage:
    """A message the bot 'sent' — supports edit/delete like PTB's Message."""

    def __init__(self, text=None, chat_id=None, message_id=None):
        self.message_id = message_id if message_id is not None else next(_ids)
        self.text = text
        self.chat_id = chat_id
        self.edits: list[str] = []
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeUserMessage(FakeMessage):
    """The incoming user message handlers reply to."""

    def __init__(self, text="", chat_id=10, message_id=500, bot=None):
        super().__init__(text=text, chat_id=chat_id, message_id=message_id)
        self.replies: list[str] = []
        self.reply_messages: list[FakeMessage] = []
        self.documents: list[tuple[str, bytes]] = []
        self.photos: list[tuple[object, str | None]] = []
        self.voices: list[bytes] = []
        self.chat_actions: list[str] = []
        self.delete_raises = False

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        msg = FakeMessage(text=text, chat_id=self.chat_id)
        self.reply_messages.append(msg)
        return msg

    async def reply_document(self, document, filename=None, **kw):
        data = document.read() if hasattr(document, "read") else document
        self.documents.append((filename, data))
        return FakeMessage(chat_id=self.chat_id)

    async def reply_photo(self, photo, caption=None, **kw):
        data = photo.read() if hasattr(photo, "read") else photo
        self.photos.append((data, caption))
        return FakeMessage(chat_id=self.chat_id)

    async def reply_voice(self, voice, **kw):
        self.voices.append(voice)
        return FakeMessage(chat_id=self.chat_id)

    async def reply_chat_action(self, action, **kw):
        self.chat_actions.append(action)

    async def delete(self):
        if self.delete_raises:
            raise RuntimeError("message can't be deleted")
        self.deleted = True


class FakeChat:
    def __init__(self, chat_id, bot=None):
        self.id = chat_id
        self.sent: list[str] = []

    async def send_message(self, text, **kw):
        self.sent.append(text)
        return FakeMessage(text=text, chat_id=self.id)


class FakeBot:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.messages: list[FakeMessage] = []  # successful send_message results
        self.fail_md_markers: list[str] = []  # MarkdownV2 sends containing these fail

    def _record(self, method, **kw):
        self.calls.append((method, kw))

    def calls_of(self, method):
        return [kw for m, kw in self.calls if m == method]

    async def send_message(self, chat_id=None, text=None, **kw):
        if kw.get("parse_mode") == "MarkdownV2" and any(
            m in text for m in self.fail_md_markers
        ):
            self._record("send_message_failed", chat_id=chat_id, text=text, **kw)
            raise RuntimeError("Bad Request: can't parse entities")
        self._record("send_message", chat_id=chat_id, text=text, **kw)
        msg = FakeMessage(text=text, chat_id=chat_id)
        self.messages.append(msg)
        return msg

    async def send_photo(self, chat_id=None, photo=None, caption=None, **kw):
        data = photo.read() if hasattr(photo, "read") else photo
        self._record("send_photo", chat_id=chat_id, photo=data, caption=caption)
        return FakeMessage(chat_id=chat_id)

    async def send_document(
        self, chat_id=None, document=None, filename=None, caption=None, **kw
    ):
        data = document.read() if hasattr(document, "read") else document
        self._record(
            "send_document",
            chat_id=chat_id,
            document=data,
            filename=filename,
            caption=caption,
        )
        return FakeMessage(chat_id=chat_id)

    async def set_message_reaction(
        self, chat_id=None, message_id=None, reaction=None, **kw
    ):
        emojis = [getattr(r, "emoji", None) for r in (reaction or [])]
        self._record(
            "set_message_reaction",
            chat_id=chat_id,
            message_id=message_id,
            emojis=emojis,
        )

    def reactions(self):
        return [kw["emojis"] for kw in self.calls_of("set_message_reaction")]


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[object] = []
        self.edited: list[str] = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


def make_update(bot, uid=1, chat_id=10, text="", message_id=500):
    msg = FakeUserMessage(text=text, chat_id=chat_id, message_id=message_id)
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=FakeChat(chat_id),
        message=msg,
        callback_query=None,
        message_reaction=None,
    )
    upd.get_bot = lambda: bot
    return upd


def make_ctx(bot, args=None):
    return SimpleNamespace(bot=bot, args=args or [])


def E(emoji):
    return SimpleNamespace(emoji=emoji)


def make_reaction_update(uid=1, chat_id=10, message_id=777, new=(), old=()):
    return SimpleNamespace(
        message_reaction=SimpleNamespace(
            user=SimpleNamespace(id=uid),
            chat=SimpleNamespace(id=chat_id),
            message_id=message_id,
            new_reaction=[E(e) for e in new],
            old_reaction=[E(e) for e in old],
        )
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Fresh caches, workspace dir present, no stale credentials file."""
    monkeypatch.setattr(main, "_SENT_CACHE", {})
    monkeypatch.setattr(main, "_CB_CACHE", {})
    main.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    if main.CLAUDE_CREDS.exists():
        main.CLAUDE_CREDS.unlink()
    yield
    if main.CLAUDE_CREDS.exists():
        main.CLAUDE_CREDS.unlink()


# ---------------------------------------------------------------------------
# send_md
# ---------------------------------------------------------------------------

def test_send_md_simple_markdown_and_cache():
    bot = FakeBot()
    asyncio.run(main.send_md(bot, 10, "hello world"))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert sends[0]["text"] == "hello world"
    assert sends[0]["parse_mode"] == "MarkdownV2"
    assert sends[0]["disable_web_page_preview"] is True
    # original (pre-conversion) chunk is cached under the sent message id
    assert main._SENT_CACHE[bot.messages[0].message_id] == "hello world"


def test_send_md_escapes_via_markdown_v2():
    bot = FakeBot()
    asyncio.run(main.send_md(bot, 10, "a.b"))
    assert bot.calls_of("send_message")[0]["text"] == "a\\.b"
    # cache keeps the ORIGINAL text, not the escaped one
    assert list(main._SENT_CACHE.values()) == ["a.b"]


def test_send_md_fallback_only_for_failing_chunk():
    bot = FakeBot()
    bot.fail_md_markers = ["B"]
    text = "A" * 2900 + "\n\n" + "B" * 2900
    asyncio.run(main.send_md(bot, 10, text))
    ok = bot.calls_of("send_message")
    failed = bot.calls_of("send_message_failed")
    # chunk A: one MarkdownV2 send; chunk B: failed MarkdownV2 then plain
    assert len(ok) == 2 and len(failed) == 1
    assert ok[0]["text"].startswith("A") and ok[0]["parse_mode"] == "MarkdownV2"
    assert failed[0]["text"].startswith("B")
    assert ok[1]["text"].startswith("B") and "parse_mode" not in ok[1]
    # chunk A was never re-sent
    assert sum(1 for kw in ok if kw["text"].startswith("A")) == 1
    # both chunks cached (original text)
    cached = sorted(main._SENT_CACHE.values())
    assert cached[0].startswith("A") and cached[1].startswith("B")


def test_send_md_oversized_after_escape_goes_plain():
    bot = FakeBot()
    # 2500 dots fit in one 3000-char chunk but escape to 5000 chars > 4000
    text = "." * 2500
    asyncio.run(main.send_md(bot, 10, text))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert sends[0]["text"] == text
    assert "parse_mode" not in sends[0]
    assert bot.calls_of("send_message_failed") == []


def test_send_md_empty_text():
    bot = FakeBot()
    asyncio.run(main.send_md(bot, 10, ""))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert "no response" in sends[0]["text"]


# ---------------------------------------------------------------------------
# run_claude_turn
# ---------------------------------------------------------------------------

def test_run_claude_turn_success(monkeypatch):
    bot = FakeBot()

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        return "hello"

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    result = asyncio.run(main.run_claude_turn(bot, 10, 1, "hi", react_to=500))
    assert result == "hello"
    # 👀 while working, 👍 when done, on the user's message
    assert bot.reactions() == [["👀"], ["👍"]]
    for kw in bot.calls_of("set_message_reaction"):
        assert kw["message_id"] == 500
    # status message created then deleted
    status = bot.messages[0]
    assert status.text == "🤔 thinking…"
    assert status.deleted is True
    # reply sent
    texts = [kw["text"] for kw in bot.calls_of("send_message")]
    assert "hello" in texts


def test_run_claude_turn_auth_error(monkeypatch):
    bot = FakeBot()

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        raise ClaudeSessionError("API error: 401 unauthorized")

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    result = asyncio.run(main.run_claude_turn(bot, 10, 1, "hi", react_to=500))
    assert result is None
    assert bot.reactions() == [["👀"], ["💔"]]
    status = bot.messages[0]
    assert status.deleted is False
    assert "/login" in status.edits[-1]
    assert "authentication failed" in status.edits[-1]


def test_run_claude_turn_generic_error(monkeypatch):
    bot = FakeBot()

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        raise ValueError("boom")

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    result = asyncio.run(main.run_claude_turn(bot, 10, 1, "hi", react_to=500))
    assert result is None
    assert bot.reactions()[-1] == ["💔"]
    assert bot.messages[0].edits[-1] == "Error: boom"


def test_run_claude_turn_tgquestion(monkeypatch):
    bot = FakeBot()
    resp = (
        "Body text.\n"
        'TGQUESTION: {"question": "Pick one", "options": ["A", "B"]}'
    )

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        return resp

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    result = asyncio.run(main.run_claude_turn(bot, 10, 1, "hi"))
    # returned/sent text has the marker stripped
    assert result == "Body text."
    sends = bot.calls_of("send_message")
    body_sends = [kw for kw in sends if "TGQUESTION" in (kw["text"] or "")]
    assert body_sends == []
    q = [kw for kw in sends if (kw["text"] or "").startswith("❓")]
    assert len(q) == 1
    assert q[0]["text"] == "❓ Pick one"
    keyboard = q[0]["reply_markup"].inline_keyboard
    assert len(keyboard) == 2
    btn0 = keyboard[0][0]
    assert btn0.text == "A"
    assert btn0.callback_data.startswith("tgq:")
    token = btn0.callback_data.split(":")[1]
    assert main._CB_CACHE[token] == ("Pick one", ["A", "B"], 10, 1)
    assert keyboard[1][0].callback_data == f"tgq:{token}:1"


def test_run_claude_turn_mentioned_files(monkeypatch):
    bot = FakeBot()
    target = main.WORKSPACE_DIR / "notes.md"
    target.write_text("notes")
    resp = f"I wrote the file at {target}."

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        return resp

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    asyncio.run(main.run_claude_turn(bot, 10, 1, "hi"))
    sends = bot.calls_of("send_message")
    mf = [kw for kw in sends if kw["text"] == "Mentioned files:"]
    assert len(mf) == 1
    keyboard = mf[0]["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    btn = keyboard[0][0]
    assert btn.text == "📎 notes.md"
    assert btn.callback_data.startswith("fget:")
    token = btn.callback_data.split(":", 1)[1]
    assert main._CB_CACHE[token] == str(target)


def test_run_claude_turn_busy_shows_queued(monkeypatch):
    bot = FakeBot()

    async def fake_ask(uid, prompt, on_tool_use=None, on_text=None):
        return "done"

    monkeypatch.setattr(main.claude, "ask", fake_ask)
    monkeypatch.setattr(main.claude, "busy", lambda uid: True)
    result = asyncio.run(main.run_claude_turn(bot, 10, 1, "hi"))
    assert result == "done"
    assert bot.messages[0].text.startswith("📥 Queued")


# ---------------------------------------------------------------------------
# on_tgq_callback
# ---------------------------------------------------------------------------

def test_tgq_callback_valid_choice(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(b, chat_id, uid, prompt, react_to=None):
        turns.append((chat_id, uid, prompt))

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    token = main._cb_store(("Pick one", ["A", "B"], 10, 1))
    query = FakeCallbackQuery(f"tgq:{token}:1")
    upd = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=1)
    )
    asyncio.run(main.on_tgq_callback(upd, make_ctx(bot)))
    assert query.answers == [None]
    assert query.edited == ["❓ Pick one\n➡️ B"]
    assert turns == [(10, 1, "B")]
    # token is consumed
    assert token not in main._CB_CACHE


def test_tgq_callback_expired_token(monkeypatch):
    bot = FakeBot()
    called = []

    async def fake_turn(*a, **kw):
        called.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    query = FakeCallbackQuery("tgq:deadbeefcafe:0")
    upd = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
    asyncio.run(main.on_tgq_callback(upd, make_ctx(bot)))
    assert query.answers == ["Expired — type your answer instead."]
    assert called == []


def test_tgq_callback_bad_index(monkeypatch):
    bot = FakeBot()
    called = []

    async def fake_turn(*a, **kw):
        called.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    token = main._cb_store(("Q", ["A", "B"], 10, 1))
    query = FakeCallbackQuery(f"tgq:{token}:5")
    upd = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
    asyncio.run(main.on_tgq_callback(upd, make_ctx(bot)))
    assert query.answers == ["Bad option."]
    assert called == []


def test_tgq_callback_non_int_index():
    query = FakeCallbackQuery("tgq:sometoken:x")
    upd = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
    asyncio.run(main.on_tgq_callback(upd, make_ctx(FakeBot())))
    assert query.answers == ["Bad data."]


def test_tgq_callback_foreign_user(monkeypatch):
    called = []

    async def fake_turn(*a, **kw):
        called.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    token = main._cb_store(("Q", ["A", "B"], 10, 1))
    query = FakeCallbackQuery(f"tgq:{token}:0")
    upd = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999))
    asyncio.run(main.on_tgq_callback(upd, make_ctx(FakeBot())))
    assert query.answers == ["Not authorized."]
    assert called == []
    # payload not consumed by a foreign user
    assert token in main._CB_CACHE


# ---------------------------------------------------------------------------
# on_fget_callback
# ---------------------------------------------------------------------------

def test_fget_callback_valid_sends_file():
    bot = FakeBot()
    target = main.WORKSPACE_DIR / "doc.txt"
    target.write_bytes(b"file-content")
    token = main._cb_store(str(target))
    query = FakeCallbackQuery(f"fget:{token}")
    upd = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
        effective_chat=FakeChat(10),
    )
    asyncio.run(main.on_fget_callback(upd, make_ctx(bot)))
    assert query.answers == [None]
    docs = bot.calls_of("send_document")
    assert len(docs) == 1
    assert docs[0]["filename"] == "doc.txt"
    assert docs[0]["document"] == b"file-content"
    assert docs[0]["chat_id"] == 10


def test_fget_callback_expired():
    bot = FakeBot()
    query = FakeCallbackQuery("fget:unknowntoken")
    upd = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
        effective_chat=FakeChat(10),
    )
    asyncio.run(main.on_fget_callback(upd, make_ctx(bot)))
    assert query.answers == ["Expired — use /get <path>."]
    assert bot.calls_of("send_document") == []


def test_fget_callback_foreign_user():
    bot = FakeBot()
    token = main._cb_store(str(main.WORKSPACE_DIR / "doc.txt"))
    query = FakeCallbackQuery(f"fget:{token}")
    upd = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999),
        effective_chat=FakeChat(10),
    )
    asyncio.run(main.on_fget_callback(upd, make_ctx(bot)))
    assert query.answers == ["Not authorized."]
    assert bot.calls_of("send_document") == []


# ---------------------------------------------------------------------------
# on_reaction
# ---------------------------------------------------------------------------

def test_reaction_thumbs_up_approves(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(b, chat_id, uid, prompt, react_to=None):
        turns.append((chat_id, uid, prompt))

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    monkeypatch.setattr(main, "is_authed", lambda: True)
    main._remember_sent(777, "the plan is X")
    upd = make_reaction_update(message_id=777, new=["👍"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert len(turns) == 1
    chat_id, uid, prompt = turns[0]
    assert (chat_id, uid) == (10, 1)
    assert "👍" in prompt
    assert "the plan is X" in prompt
    assert "Proceed accordingly" in prompt


def test_reaction_thumbs_down_reconsiders(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(b, chat_id, uid, prompt, react_to=None):
        turns.append(prompt)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    monkeypatch.setattr(main, "is_authed", lambda: True)
    main._remember_sent(778, "bad idea")
    upd = make_reaction_update(message_id=778, new=["👎"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert len(turns) == 1
    assert "👎" in turns[0]
    assert "bad idea" in turns[0]
    assert "Reconsider" in turns[0]


def test_reaction_thumbs_up_uncached_uses_placeholder(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(b, chat_id, uid, prompt, react_to=None):
        turns.append(prompt)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    monkeypatch.setattr(main, "is_authed", lambda: True)
    upd = make_reaction_update(message_id=999999, new=["👍"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert len(turns) == 1
    assert "(message not cached — too old)" in turns[0]


def test_reaction_thumbs_up_unauthenticated_login_hint(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(*a, **kw):
        turns.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    main._remember_sent(777, "text")
    upd = make_reaction_update(message_id=777, new=["👍"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert turns == []
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert "not authenticated" in sends[0]["text"]
    assert "/login" in sends[0]["text"]


def test_reaction_heart_saves_to_workspace():
    bot = FakeBot()
    main._remember_sent(780, "keep this insight")
    upd = make_reaction_update(message_id=780, new=["❤"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert sends[0]["text"].startswith("💾 Saved to ")
    saved_path = Path(sends[0]["text"].removeprefix("💾 Saved to "))
    assert saved_path.parent == main.WORKSPACE_DIR / main.SAVED_DIR_NAME
    assert saved_path.read_text() == "keep this insight\n"


def test_reaction_fire_saves_too():
    bot = FakeBot()
    main._remember_sent(781, "fire content")
    upd = make_reaction_update(message_id=781, new=["🔥"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1 and sends[0]["text"].startswith("💾 Saved to ")


def test_reaction_save_uncached_too_old():
    bot = FakeBot()
    upd = make_reaction_update(message_id=424242, new=["❤"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert "too old" in sends[0]["text"]


def test_reaction_removal_ignored(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(*a, **kw):
        turns.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    monkeypatch.setattr(main, "is_authed", lambda: True)
    main._remember_sent(782, "x")
    upd = make_reaction_update(message_id=782, new=[], old=["👍"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert turns == []
    assert bot.calls == []


def test_reaction_foreign_user_ignored(monkeypatch):
    bot = FakeBot()
    turns = []

    async def fake_turn(*a, **kw):
        turns.append(a)

    monkeypatch.setattr(main, "run_claude_turn", fake_turn)
    monkeypatch.setattr(main, "is_authed", lambda: True)
    main._remember_sent(783, "x")
    upd = make_reaction_update(uid=999, message_id=783, new=["👍"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert turns == []
    assert bot.calls == []


def test_reaction_other_emoji_ignored():
    bot = FakeBot()
    main._remember_sent(784, "x")
    upd = make_reaction_update(message_id=784, new=["🎉"])
    asyncio.run(main.on_reaction(upd, make_ctx(bot)))
    assert bot.calls == []


# ---------------------------------------------------------------------------
# handle_text — credentials paste & unauthenticated hint
# ---------------------------------------------------------------------------

CREDS = json.dumps({"claudeAiOauth": {"accessToken": "tok", "refreshToken": "r"}})


def test_handle_text_credentials_saved_and_deleted():
    bot = FakeBot()
    upd = make_update(bot, text=CREDS)
    asyncio.run(main.handle_text(upd, make_ctx(bot)))
    assert main.CLAUDE_CREDS.read_text() == CREDS
    assert upd.message.deleted is True
    assert len(upd.effective_chat.sent) == 1
    note = upd.effective_chat.sent[0]
    assert "Credentials saved" in note
    assert "deleted for safety" in note


def test_handle_text_credentials_delete_fails_warns():
    bot = FakeBot()
    upd = make_update(bot, text=CREDS)
    upd.message.delete_raises = True
    asyncio.run(main.handle_text(upd, make_ctx(bot)))
    assert main.CLAUDE_CREDS.read_text() == CREDS
    note = upd.effective_chat.sent[0]
    assert "Credentials saved" in note
    assert "remove it manually" in note


def test_handle_text_credentials_invalid_json():
    bot = FakeBot()
    upd = make_update(bot, text='{"claudeAiOauth": broken')
    asyncio.run(main.handle_text(upd, make_ctx(bot)))
    assert not main.CLAUDE_CREDS.exists()
    assert len(upd.message.replies) == 1
    assert upd.message.replies[0].startswith("Invalid JSON:")
    assert upd.effective_chat.sent == []


def test_handle_text_unauthenticated_login_hint():
    bot = FakeBot()
    upd = make_update(bot, text="hello claude")
    asyncio.run(main.handle_text(upd, make_ctx(bot)))
    assert upd.message.replies == ["Claude not authenticated. Use /login."]


def test_handle_text_foreign_user_ignored():
    bot = FakeBot()
    upd = make_update(bot, uid=999, text="hello")
    asyncio.run(main.handle_text(upd, make_ctx(bot)))
    assert upd.message.replies == []
    assert bot.calls == []


# ---------------------------------------------------------------------------
# cmd_cancel
# ---------------------------------------------------------------------------

def test_cmd_cancel_busy(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main.claude, "cancel", lambda uid: True)
    upd = make_update(bot, text="/cancel")
    asyncio.run(main.cmd_cancel(upd, make_ctx(bot)))
    assert upd.message.replies[0].startswith("⏹ Cancelled")


def test_cmd_cancel_idle(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main.claude, "cancel", lambda uid: False)
    upd = make_update(bot, text="/cancel")
    asyncio.run(main.cmd_cancel(upd, make_ctx(bot)))
    assert upd.message.replies == ["Nothing is running."]


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_output(monkeypatch):
    bot = FakeBot()
    fake_session = SimpleNamespace(
        session_id="sess-42",
        last_result={
            "total_cost_usd": 0.1234,
            "duration_ms": 2500,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        total_cost_usd=0.5,
    )
    monkeypatch.setattr(main.claude, "_sessions", {"main": fake_session})
    monkeypatch.setattr(main.claude, "_current", "main")
    upd = make_update(bot, text="/status")
    asyncio.run(main.cmd_status(upd, make_ctx(bot)))
    text = upd.message.replies[0]
    assert "Auth: ❌ — use /login" in text  # no creds file
    assert "Model: default" in text
    assert "Session: main (sess-42)" in text
    assert "Running: no" in text
    assert "Last turn: $0.1234, 2s, 10→20 tok" in text
    assert "Session cost: $0.5000" in text
    assert "Bot uptime:" in text


def test_cmd_status_no_session(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main.claude, "_sessions", {})
    monkeypatch.setattr(main.claude, "_current", "main")
    upd = make_update(bot, text="/status")
    asyncio.run(main.cmd_status(upd, make_ctx(bot)))
    text = upd.message.replies[0]
    assert "Session: main (no id yet)" in text
    assert "Session cost: $0.0000" in text


def test_cmd_status_foreign_user_ignored():
    bot = FakeBot()
    upd = make_update(bot, uid=999, text="/status")
    asyncio.run(main.cmd_status(upd, make_ctx(bot)))
    assert upd.message.replies == []


# ---------------------------------------------------------------------------
# cmd_logs
# ---------------------------------------------------------------------------

def _set_log_lines(monkeypatch, lines):
    monkeypatch.setattr(main._ring, "buf", deque(lines, maxlen=400))


def test_cmd_logs_default_50(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, [f"line-{i:03d}" for i in range(100)])
    upd = make_update(bot, text="/logs")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot)))
    text = upd.message.replies[0]
    lines = text.split("\n")
    assert len(lines) == 50
    assert lines[0] == "line-050"
    assert lines[-1] == "line-099"


def test_cmd_logs_arg_clamped_low(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, [f"line-{i:03d}" for i in range(100)])
    upd = make_update(bot, text="/logs 0")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot, args=["0"])))
    assert upd.message.replies[0] == "line-099"  # clamped to 1


def test_cmd_logs_arg_clamped_high(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, [f"line-{i:03d}" for i in range(100)])
    upd = make_update(bot, text="/logs 99999")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot, args=["99999"])))
    # clamped to 400 -> all 100 available lines
    assert len(upd.message.replies[0].split("\n")) == 100


def test_cmd_logs_bad_arg_usage(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, ["x"])
    upd = make_update(bot, text="/logs abc")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot, args=["abc"])))
    assert upd.message.replies[0].startswith("Usage: /logs")


def test_cmd_logs_empty(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, [])
    upd = make_update(bot, text="/logs")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot)))
    assert upd.message.replies == ["No logs yet."]


def test_cmd_logs_long_output_as_document(monkeypatch):
    bot = FakeBot()
    _set_log_lines(monkeypatch, ["Z" * 100 for _ in range(50)])
    upd = make_update(bot, text="/logs")
    asyncio.run(main.cmd_logs(upd, make_ctx(bot)))
    assert upd.message.replies == []
    assert len(upd.message.documents) == 1
    filename, data = upd.message.documents[0]
    assert filename == "bot.log"
    assert data.decode().count("Z" * 100) == 50


# ---------------------------------------------------------------------------
# cmd_get
# ---------------------------------------------------------------------------

def test_cmd_get_no_args_usage():
    bot = FakeBot()
    upd = make_update(bot, text="/get")
    asyncio.run(main.cmd_get(upd, make_ctx(bot)))
    assert upd.message.replies[0].startswith("Usage: /get <path>")


def test_cmd_get_traversal_rejected():
    bot = FakeBot()
    upd = make_update(bot, text="/get ../../etc/passwd")
    asyncio.run(main.cmd_get(upd, make_ctx(bot, args=["../../etc/passwd"])))
    assert "must be under" in upd.message.replies[0]
    assert bot.calls_of("send_document") == []


def test_cmd_get_existing_file_sent():
    bot = FakeBot()
    (main.WORKSPACE_DIR / "hello.txt").write_bytes(b"hi there")
    upd = make_update(bot, text="/get hello.txt")
    asyncio.run(main.cmd_get(upd, make_ctx(bot, args=["hello.txt"])))
    docs = bot.calls_of("send_document")
    assert len(docs) == 1
    assert docs[0]["filename"] == "hello.txt"
    assert docs[0]["document"] == b"hi there"


def test_cmd_get_missing_file_reports_not_found():
    bot = FakeBot()
    upd = make_update(bot, text="/get nope.txt")
    asyncio.run(main.cmd_get(upd, make_ctx(bot, args=["nope.txt"])))
    sends = bot.calls_of("send_message")
    assert len(sends) == 1
    assert sends[0]["text"].startswith("Not found:")


# ---------------------------------------------------------------------------
# cmd_unknown
# ---------------------------------------------------------------------------

def test_cmd_unknown():
    bot = FakeBot()
    upd = make_update(bot, text="/bogus")
    asyncio.run(main.cmd_unknown(upd, make_ctx(bot)))
    assert upd.message.replies == ["Unknown command. Use /help."]


def test_cmd_unknown_foreign_ignored():
    bot = FakeBot()
    upd = make_update(bot, uid=42, text="/bogus")
    asyncio.run(main.cmd_unknown(upd, make_ctx(bot)))
    assert upd.message.replies == []


# ---------------------------------------------------------------------------
# cmd_img
# ---------------------------------------------------------------------------

def test_cmd_img_no_openai_key(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main, "_openai_client", None)
    upd = make_update(bot, text="/img a cat")
    asyncio.run(main.cmd_img(upd, make_ctx(bot, args=["a", "cat"])))
    assert "OPENAI_API_KEY" in upd.message.replies[0]
    assert upd.message.photos == []


def test_cmd_img_generates_photo(monkeypatch):
    bot = FakeBot()
    png = b"\x89PNG-fake-bytes"
    seen_params = {}

    class FakeImages:
        def generate(self, **params):
            seen_params.update(params)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(png).decode())]
            )

    monkeypatch.setattr(
        main, "_openai_client", SimpleNamespace(images=FakeImages())
    )
    upd = make_update(bot, text="/img a cat")
    asyncio.run(main.cmd_img(upd, make_ctx(bot, args=["a", "cat"])))
    # status message created and deleted
    assert upd.message.replies == ["🎨 Generating image…"]
    assert upd.message.reply_messages[0].deleted is True
    assert len(upd.message.photos) == 1
    data, caption = upd.message.photos[0]
    assert data == png
    assert caption == "a cat"
    assert seen_params["prompt"] == "a cat"
    # gpt-image-1 (default) must not get response_format
    assert "response_format" not in seen_params


def test_cmd_img_no_args_usage(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main, "_openai_client", SimpleNamespace())
    upd = make_update(bot, text="/img")
    asyncio.run(main.cmd_img(upd, make_ctx(bot)))
    assert upd.message.replies == ["Usage: /img <description of the image>"]


# ---------------------------------------------------------------------------
# cmd_export & _transcript_markdown
# ---------------------------------------------------------------------------

def _fake_session(sid):
    return SimpleNamespace(session_id=sid, last_result=None, total_cost_usd=0.0)


def test_cmd_export_no_session(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(main.claude, "_sessions", {})
    monkeypatch.setattr(main.claude, "_current", "main")
    upd = make_update(bot, text="/export")
    asyncio.run(main.cmd_export(upd, make_ctx(bot)))
    assert upd.message.replies == ["No active session to export."]


def test_cmd_export_transcript_missing(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(
        main.claude, "_sessions", {"main": _fake_session("no-such-session")}
    )
    monkeypatch.setattr(main.claude, "_current", "main")
    (main.CLAUDE_HOME / "projects" / "p").mkdir(parents=True, exist_ok=True)
    upd = make_update(bot, text="/export")
    asyncio.run(main.cmd_export(upd, make_ctx(bot)))
    assert "not found" in upd.message.replies[0]


def test_cmd_export_sends_transcript_document(monkeypatch):
    bot = FakeBot()
    sid = "abcd1234efgh5678"
    proj = main.CLAUDE_HOME / "projects" / "proj1"
    proj.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "user", "message": {"content": "Hello Claude"}},
        # tool_result noise: no text blocks, must be skipped entirely
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "NOISE-BLOB"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Hi there"},
                    {"type": "tool_use", "name": "Bash"},
                ]
            },
        },
        {"type": "system", "subtype": "init"},  # ignored
        "not-json-at-all",  # unparseable line, skipped
    ]
    lines = []
    for e in entries:
        lines.append(e if isinstance(e, str) else json.dumps(e))
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(main.claude, "_sessions", {"main": _fake_session(sid)})
    monkeypatch.setattr(main.claude, "_current", "main")
    upd = make_update(bot, text="/export")
    asyncio.run(main.cmd_export(upd, make_ctx(bot)))
    assert len(upd.message.documents) == 1
    filename, data = upd.message.documents[0]
    assert filename == f"session-{sid[:8]}.md"
    doc = data.decode()
    assert doc.startswith(f"# Session {sid}")
    assert "## 👤 User\n\nHello Claude" in doc
    assert "## 🤖 Claude\n\nHi there" in doc
    assert "> 🔧 Bash" in doc
    assert "NOISE-BLOB" not in doc


def test_transcript_markdown_returns_none_without_projects():
    assert main._transcript_markdown("zzz-does-not-exist") is None
