"""Tests for the Telegram-UI feature batch: expandable blockquotes, quick
keyboard, pinned status, quote-reply context, forum-topic sessions, poll
questions, edited-message re-run, actionable notify, /img albums, effects."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from bot import main as m
from bot.markdown_v2 import to_telegram_markdown


class FakeBot:
    def __init__(self):
        self.calls = []

    def _rec(self, method, **kw):
        self.calls.append((method, kw))

    def of(self, method):
        return [kw for meth, kw in self.calls if meth == method]

    async def send_message(self, chat_id=None, text=None, **kw):
        self._rec("send_message", chat_id=chat_id, text=text, **kw)
        return SimpleNamespace(message_id=len(self.calls) + 900)

    async def send_poll(self, chat_id=None, question=None, options=None, **kw):
        self._rec("send_poll", chat_id=chat_id, question=question, options=options, **kw)
        return SimpleNamespace(
            message_id=777, poll=SimpleNamespace(id="poll-1")
        )

    async def stop_poll(self, chat_id=None, message_id=None, **kw):
        self._rec("stop_poll", chat_id=chat_id, message_id=message_id)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self._rec("edit_message_text", chat_id=chat_id, message_id=message_id, text=text)

    async def pin_chat_message(self, chat_id=None, message_id=None, **kw):
        self._rec("pin_chat_message", chat_id=chat_id, message_id=message_id)

    async def unpin_chat_message(self, chat_id=None, message_id=None, **kw):
        self._rec("unpin_chat_message", chat_id=chat_id, message_id=message_id)

    async def set_message_reaction(self, chat_id=None, message_id=None, reaction=None, **kw):
        self._rec(
            "set_message_reaction",
            message_id=message_id,
            emojis=[getattr(r, "emoji", None) for r in (reaction or [])],
            is_big=kw.get("is_big"),
        )


class FakeUserMessage:
    def __init__(self, text="", chat_id=10):
        self.text = text
        self.chat_id = chat_id
        self.message_id = 500
        self.replies = []
        self.reply_markups = []
        self.media_groups = []
        self.photos = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.reply_markups.append(kw.get("reply_markup"))
        return SimpleNamespace(message_id=601)

    async def reply_photo(self, photo, caption=None, **kw):
        self.photos.append((photo, caption))

    async def reply_media_group(self, media, **kw):
        self.media_groups.append(media)


def make_update(text="", uid=1, chat_id=10):
    msg = FakeUserMessage(text=text, chat_id=chat_id)
    upd = SimpleNamespace(
        message=msg,
        edited_message=None,
        effective_message=msg,
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return upd


def make_ctx(bot=None, args=None):
    return SimpleNamespace(bot=bot or FakeBot(), args=args or [])


# ---------------------------------------------------------------------------
# blockquotes
# ---------------------------------------------------------------------------

class TestBlockquotes:
    def test_short_quote_plain(self):
        out = to_telegram_markdown("> uno\n> due")
        assert out == ">uno\n>due"

    def test_long_quote_expandable(self):
        out = to_telegram_markdown("> a\n> b\n> c\n> d")
        assert out.startswith("**>a")
        assert out.endswith("||")
        assert out.count(">") == 4

    def test_quote_content_escaped(self):
        out = to_telegram_markdown("> log [x] done.")
        assert out == ">log \\[x\\] done\\."

    def test_quote_between_paragraphs(self):
        out = to_telegram_markdown("prima\n> quote\ndopo")
        assert ">quote" in out
        assert "prima" in out and "dopo" in out


# ---------------------------------------------------------------------------
# quick keyboard
# ---------------------------------------------------------------------------

class TestQuick:
    @pytest.fixture(autouse=True)
    def _file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "QUICK_FILE", tmp_path / "quick.json")

    def test_add_and_list(self):
        upd = make_update()
        asyncio.run(m.cmd_quick(upd, make_ctx(args=["add", "stato", "server"])))
        assert "Added. (1/12)" in upd.message.replies[0]
        assert m._load_quick() == ["stato server"]
        kb = upd.message.reply_markups[0]
        assert kb.keyboard[0][0].text == "stato server"

    def test_rm(self):
        m._save_quick(["a", "b"])
        upd = make_update()
        asyncio.run(m.cmd_quick(upd, make_ctx(args=["rm", "1"])))
        assert "Removed: a" in upd.message.replies[0]
        assert m._load_quick() == ["b"]

    def test_full_keyboard_rejected(self):
        m._save_quick([f"p{i}" for i in range(12)])
        upd = make_update()
        asyncio.run(m.cmd_quick(upd, make_ctx(args=["add", "x"])))
        assert "full" in upd.message.replies[0]

    def test_empty_list_hint(self):
        upd = make_update()
        asyncio.run(m.cmd_quick(upd, make_ctx()))
        assert "No quick prompts" in upd.message.replies[0]

    def test_keyboard_rows_of_two(self):
        m._save_quick(["a", "b", "c"])
        kb = m._quick_keyboard()
        assert [len(r) for r in kb.keyboard] == [2, 1]


# ---------------------------------------------------------------------------
# pinned status
# ---------------------------------------------------------------------------

class TestPinnedStatus:
    @pytest.fixture(autouse=True)
    def _file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "PIN_FILE", tmp_path / "pinned.json")
        m._last_pin_text["text"] = ""

    def test_pin_creates_and_stores(self):
        bot = FakeBot()
        upd = make_update()
        asyncio.run(m.cmd_status(upd, make_ctx(bot, args=["pin"])))
        assert bot.of("pin_chat_message")
        pin = m._load_pin()
        assert pin["chat_id"] == 10 and pin["message_id"] == 601

    def test_updater_edits_only_on_change(self):
        m.PIN_FILE.write_text(json.dumps({"chat_id": 10, "message_id": 601}))
        bot = FakeBot()
        ctx = SimpleNamespace(bot=bot)
        asyncio.run(m.update_pinned(ctx))
        assert len(bot.of("edit_message_text")) == 1
        # same text again -> no second edit
        text = m._last_pin_text["text"]
        asyncio.run(m.update_pinned(ctx))
        assert len(bot.of("edit_message_text")) == 1
        assert m._last_pin_text["text"] == text

    def test_unpin(self):
        m.PIN_FILE.write_text(json.dumps({"chat_id": 10, "message_id": 601}))
        bot = FakeBot()
        upd = make_update()
        asyncio.run(m.cmd_status(upd, make_ctx(bot, args=["unpin"])))
        assert bot.of("unpin_chat_message")
        assert m._load_pin() is None

    def test_no_pin_noop(self):
        bot = FakeBot()
        asyncio.run(m.update_pinned(SimpleNamespace(bot=bot)))
        assert bot.calls == []


# ---------------------------------------------------------------------------
# quote-reply context
# ---------------------------------------------------------------------------

class TestReplyContext:
    def _msg(self, text="dimmi di più", quote=None, replied=None):
        return SimpleNamespace(
            text=text, quote=quote, reply_to_message=replied
        )

    def test_no_reply_passthrough(self):
        msg = self._msg()
        assert m._with_reply_context(msg, "ciao") == "ciao"

    def test_quote_slice_wins(self):
        msg = self._msg(
            quote=SimpleNamespace(text="solo questa riga"),
            replied=SimpleNamespace(message_id=1, text="tutto il messaggio"),
        )
        out = m._with_reply_context(msg, "spiegami")
        assert "solo questa riga" in out
        assert "tutto il messaggio" not in out
        assert out.endswith("spiegami")

    def test_reply_uses_sent_cache(self, monkeypatch):
        monkeypatch.setitem(m._SENT_CACHE, 42, "risposta del bot")
        msg = self._msg(replied=SimpleNamespace(
            message_id=42, text=None, caption=None, forum_topic_created=None
        ))
        out = m._with_reply_context(msg, "riprova")
        assert "risposta del bot" in out

    def test_topic_service_reply_ignored(self):
        msg = self._msg(replied=SimpleNamespace(
            message_id=1, text="Topic", forum_topic_created=SimpleNamespace(name="x")
        ))
        assert m._with_reply_context(msg, "ciao") == "ciao"

    def test_quoted_capped_at_1000(self):
        msg = self._msg(quote=SimpleNamespace(text="x" * 5000))
        out = m._with_reply_context(msg, "ok")
        assert len(out) < 1200


# ---------------------------------------------------------------------------
# forum topics -> sessions
# ---------------------------------------------------------------------------

class TestTopicInfo:
    def test_private_chat_no_topic(self):
        upd = make_update()
        assert m._topic_info(upd) == (None, None)

    def test_topic_message_maps_to_session(self):
        msg = SimpleNamespace(is_topic_message=True, message_thread_id=99)
        upd = SimpleNamespace(effective_message=msg)
        assert m._topic_info(upd) == (99, "topic-99")

    def test_general_topic_without_thread(self):
        msg = SimpleNamespace(is_topic_message=True, message_thread_id=None)
        upd = SimpleNamespace(effective_message=msg)
        assert m._topic_info(upd) == (None, None)


# ---------------------------------------------------------------------------
# TGQUESTION multi -> poll
# ---------------------------------------------------------------------------

class TestTgqMulti:
    def test_multi_flag_parsed(self):
        text = 'x\nTGQUESTION: {"question": "Q?", "options": ["a", "b"], "multi": true}'
        _, tgq = m._extract_tgquestion(text)
        assert tgq["multi"] is True

    def test_multi_sends_poll_and_caches(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {})
        bot = FakeBot()
        tgq = {"question": "Q?", "options": ["a", "b"], "multi": True}
        asyncio.run(m._send_tgquestion(bot, 10, 1, tgq, thread_id=None))
        [poll] = bot.of("send_poll")
        assert poll["question"] == "Q?"
        assert poll["allows_multiple_answers"] is True
        assert poll["is_anonymous"] is False
        payload = m._CB_CACHE["poll-1"]
        assert payload[0] == "poll" and payload[6] == 777

    def test_single_stays_buttons(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {})
        bot = FakeBot()
        tgq = {"question": "Q?", "options": ["a", "b"], "multi": False}
        asyncio.run(m._send_tgquestion(bot, 10, 1, tgq))
        assert bot.of("send_message") and not bot.of("send_poll")

    def test_poll_answer_feeds_claude(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {})
        turns = []

        async def fake_turn(bot, chat_id, uid, prompt, **kw):
            turns.append((chat_id, uid, prompt, kw.get("thread_id")))

        monkeypatch.setattr(m, "run_claude_turn", fake_turn)
        m._CB_CACHE["poll-1"] = ("poll", "Q?", ["a", "b", "c"], 10, 1, None, 777)
        bot = FakeBot()
        upd = SimpleNamespace(
            poll_answer=SimpleNamespace(
                poll_id="poll-1",
                user=SimpleNamespace(id=1),
                option_ids=[0, 2],
            )
        )
        asyncio.run(m.on_poll_answer(upd, SimpleNamespace(bot=bot)))
        assert bot.of("stop_poll")
        assert turns == [(10, 1, 'Answer to "Q?": a, c', None)]

    def test_poll_answer_foreign_user_ignored(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {"poll-1": ("poll", "Q?", ["a"], 10, 1, None, 777)})
        upd = SimpleNamespace(
            poll_answer=SimpleNamespace(
                poll_id="poll-1", user=SimpleNamespace(id=999), option_ids=[0]
            )
        )
        asyncio.run(m.on_poll_answer(upd, SimpleNamespace(bot=FakeBot())))
        assert "poll-1" in m._CB_CACHE  # untouched


# ---------------------------------------------------------------------------
# edited message -> re-run
# ---------------------------------------------------------------------------

class TestEditedRerun:
    def _edited_update(self, text="testo corretto", uid=1):
        msg = FakeUserMessage(text=text)
        msg.is_topic_message = False
        upd = SimpleNamespace(
            edited_message=msg,
            effective_message=msg,
            effective_user=SimpleNamespace(id=uid),
        )
        return upd, msg

    def test_edit_offers_rerun_button(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {})
        upd, msg = self._edited_update()
        asyncio.run(m.on_edited(upd, make_ctx()))
        assert "re-run" in msg.replies[0].lower()
        btn = msg.reply_markups[0].inline_keyboard[0][0]
        assert btn.callback_data.startswith("rerun:")
        token = btn.callback_data.split(":", 1)[1]
        assert m._CB_CACHE[token][1] == "testo corretto"

    def test_edit_command_ignored(self, monkeypatch):
        monkeypatch.setattr(m, "_CB_CACHE", {})
        upd, msg = self._edited_update(text="/status")
        asyncio.run(m.on_edited(upd, make_ctx()))
        assert msg.replies == []

    def test_rerun_callback_runs_turn(self, monkeypatch):
        turns = []

        async def fake_turn(bot, chat_id, uid, prompt, **kw):
            turns.append((chat_id, uid, prompt, kw.get("session")))

        monkeypatch.setattr(m, "run_claude_turn", fake_turn)
        monkeypatch.setattr(m, "_CB_CACHE", {})
        token = m._cb_store(("rerun", "testo ok", 10, 1, None, None))

        class Q:
            data = f"rerun:{token}"
            def __init__(self):
                self.answers, self.edited = [], []
            async def answer(self, text=None, **kw):
                self.answers.append(text)
            async def edit_message_text(self, text, **kw):
                self.edited.append(text)

        q = Q()
        upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=1))
        asyncio.run(m.on_rerun_callback(upd, make_ctx()))
        assert turns == [(10, 1, "(corrected message) testo ok", None)]
        assert q.edited


# ---------------------------------------------------------------------------
# actionable notify
# ---------------------------------------------------------------------------

class TestNotifyButtons:
    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        (ws / ".notify").mkdir(parents=True)
        monkeypatch.setattr(m, "WORKSPACE_DIR", ws)
        monkeypatch.setattr(m, "CHAT_FILE", tmp_path / "chat.json")
        monkeypatch.setattr(m, "_CB_CACHE", {})
        m._save_chat_id(42)
        return ws

    def test_json_notify_renders_question(self, env):
        payload = {"text": "Deploy pronto", "question": "Procedo?", "options": ["sì", "no"]}
        (env / ".notify" / "q.json").write_text(json.dumps(payload))
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        texts = [kw["text"] for kw in bot.of("send_message")]
        assert any("Deploy pronto" in t for t in texts)
        assert any(t.startswith("❓ Procedo?") for t in texts)
        assert not (env / ".notify" / "q.json").exists()

    def test_json_without_valid_options_falls_back_to_text(self, env):
        (env / ".notify" / "bad.json").write_text('{"options": "not-a-list"}')
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        texts = [kw["text"] for kw in bot.of("send_message")]
        assert texts and texts[0].startswith("🔔 ")

    def test_plain_text_still_works(self, env):
        (env / ".notify" / "n.txt").write_text("ciao")
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        assert [kw["text"] for kw in bot.of("send_message")] == ["🔔 ciao"]


# ---------------------------------------------------------------------------
# /img albums
# ---------------------------------------------------------------------------

class TestImgAlbum:
    def _client(self, n_data=1):
        captured = {}

        class Images:
            def generate(self, **kw):
                captured.update(kw)
                data = [SimpleNamespace(b64_json="aGk=", url=None) for _ in range(kw.get("n", 1))]
                return SimpleNamespace(data=data)

        return SimpleNamespace(images=Images()), captured

    def test_count_parsed_and_album_sent(self, monkeypatch):
        client, captured = self._client()
        monkeypatch.setattr(m, "_openai_client", client)
        monkeypatch.setattr(m, "IMAGE_MODEL", "gpt-image-1")
        upd = make_update()
        asyncio.run(m.cmd_img(upd, make_ctx(args=["3", "un", "gatto"])))
        assert captured["n"] == 3
        assert captured["prompt"] == "un gatto"
        [group] = upd.message.media_groups
        assert len(group) == 3
        assert group[0].caption == "un gatto"
        assert group[1].caption is None

    def test_single_image_stays_photo(self, monkeypatch):
        client, _ = self._client()
        monkeypatch.setattr(m, "_openai_client", client)
        monkeypatch.setattr(m, "IMAGE_MODEL", "gpt-image-1")
        upd = make_update()
        asyncio.run(m.cmd_img(upd, make_ctx(args=["un", "gatto"])))
        assert upd.message.photos and not upd.message.media_groups

    def test_count_clamped_to_four(self, monkeypatch):
        client, captured = self._client()
        monkeypatch.setattr(m, "_openai_client", client)
        monkeypatch.setattr(m, "IMAGE_MODEL", "gpt-image-1")
        upd = make_update()
        asyncio.run(m.cmd_img(upd, make_ctx(args=["9", "x"])))
        assert captured["n"] == 4


# ---------------------------------------------------------------------------
# long-run effects
# ---------------------------------------------------------------------------

class TestLongRunCelebration:
    def test_big_reaction_and_effect_on_long_run(self, monkeypatch):
        bot = FakeBot()

        async def slow_ask(uid, prompt, **kw):
            return "fatto"

        monkeypatch.setattr(m.claude, "ask", slow_ask)
        monkeypatch.setattr(m, "LONG_RUN_S", -1)  # any duration counts as long
        asyncio.run(m.run_claude_turn(bot, 10, 1, "ciao", react_to=500))
        sends = bot.of("send_message")
        assert any(kw.get("message_effect_id") == m.EFFECT_TADA_ID for kw in sends)
        reacts = bot.of("set_message_reaction")
        assert reacts[-1]["emojis"] == ["👍"] and reacts[-1]["is_big"] is True

    def test_no_effect_on_fast_run(self, monkeypatch):
        bot = FakeBot()

        async def fast_ask(uid, prompt, **kw):
            return "fatto"

        monkeypatch.setattr(m.claude, "ask", fast_ask)
        asyncio.run(m.run_claude_turn(bot, 10, 1, "ciao", react_to=500))
        sends = bot.of("send_message")
        assert all(not kw.get("message_effect_id") for kw in sends)
        reacts = bot.of("set_message_reaction")
        assert not reacts[-1]["is_big"]
