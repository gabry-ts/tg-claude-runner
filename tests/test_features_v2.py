"""Tests for named sessions, model picker, scheduler tz/model routing and
the proactive-notify inbox."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot import main as m
from bot import claude_session as cs
from bot import scheduler as sched_mod
from bot import session_state
from bot.claude_session import ClaudeSession, sessions_in_state


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=1000 + len(self.sent))


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.reply_markups = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.reply_markups.append(kw.get("reply_markup"))
        return SimpleNamespace(message_id=1)


def make_update(text="", uid=1):
    return SimpleNamespace(
        message=FakeMessage(text),
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=42),
    )


def make_ctx(bot=None, args=None):
    return SimpleNamespace(bot=bot or FakeBot(), args=args or [])


@pytest.fixture
def state_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(session_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session_state, "STATE_FILE", tmp_path / "state.json")
    return tmp_path


# ---------------------------------------------------------------------------
# named-session state
# ---------------------------------------------------------------------------

class TestSessionsInState:
    def test_nested_shape_passthrough(self):
        s = {"sessions": {"work": {"session_id": "x"}}}
        assert sessions_in_state(s) == {"work": {"session_id": "x"}}

    def test_legacy_flat_shape_migrates_to_main(self):
        s = {"session_id": "old", "cwd": "/workspace", "model": None}
        assert sessions_in_state(s) == {
            "main": {"session_id": "old", "cwd": "/workspace", "model": None}
        }

    def test_empty_state(self):
        assert sessions_in_state({}) == {}


class TestClaudeSessionNamedPersistence:
    def test_save_writes_nested_shape(self, state_tmp, tmp_path):
        s = ClaudeSession(cwd=tmp_path, persist_state=True, state_key="work")
        s._session_id = "sid-1"
        s._save_session_id()
        state = session_state.load_state()
        assert state["sessions"]["work"]["session_id"] == "sid-1"
        assert "session_id" not in state  # no legacy keys at top level

    def test_two_keys_coexist_and_reset_clears_only_own(self, state_tmp, tmp_path):
        a = ClaudeSession(cwd=tmp_path, persist_state=True, state_key="a")
        a._session_id = "sid-a"
        a._save_session_id()
        b = ClaudeSession(cwd=tmp_path, persist_state=True, state_key="b")
        b._session_id = "sid-b"
        b._save_session_id()

        loaded_a = ClaudeSession(
            cwd=tmp_path, persist_state=True, state_key="a"
        )
        assert loaded_a.session_id == "sid-a"

        asyncio.run(a.reset())
        state = session_state.load_state()
        assert "a" not in state["sessions"]
        assert state["sessions"]["b"]["session_id"] == "sid-b"

    def test_legacy_state_loaded_as_main_and_upgraded_on_save(
        self, state_tmp, tmp_path
    ):
        session_state.save_state(
            {"session_id": "legacy", "cwd": str(tmp_path), "model": None}
        )
        s = ClaudeSession(cwd=tmp_path, persist_state=True, state_key="main")
        assert s.session_id == "legacy"
        s._session_id = "new"
        s._save_session_id()
        state = session_state.load_state()
        assert state["sessions"]["main"]["session_id"] == "new"
        assert "session_id" not in state

    def test_reset_preserves_current_marker(self, state_tmp, tmp_path):
        session_state.save_state({"current": "work"})
        s = ClaudeSession(cwd=tmp_path, persist_state=True, state_key="work")
        s._session_id = "x"
        s._save_session_id()
        asyncio.run(s.reset())
        assert session_state.load_state().get("current") == "work"


class TestRunnerSessions:
    @pytest.fixture
    def runner(self, state_tmp, monkeypatch):
        r = m.ClaudeRunner()
        monkeypatch.setattr(m, "claude", r)
        return r

    def test_default_current_is_main(self, runner):
        assert runner.current_name == "main"
        assert "main" in runner.session_names()

    def test_switch_persists_current(self, runner):
        asyncio.run(runner.switch(1, "work"))
        assert runner.current_name == "work"
        assert session_state.load_state()["current"] == "work"
        assert "work" in runner.session_names()

    def test_delete_falls_back_to_main(self, runner):
        asyncio.run(runner.switch(1, "work"))
        runner._get_session("work")._session_id = "sid"
        runner._get_session("work")._save_session_id()
        assert asyncio.run(runner.delete(1, "work")) is True
        assert runner.current_name == "main"
        assert "work" not in sessions_in_state(session_state.load_state())

    def test_delete_missing_returns_false(self, runner):
        assert asyncio.run(runner.delete(1, "ghost")) is False

    def test_status_info_has_session_name(self, runner):
        assert runner.status_info()["session_name"] == "main"


class TestCmdSession:
    @pytest.fixture
    def runner(self, state_tmp, monkeypatch):
        r = m.ClaudeRunner()
        monkeypatch.setattr(m, "claude", r)
        return r

    def test_switch_via_command(self, runner):
        upd = make_update("/session work")
        asyncio.run(m.cmd_session(upd, make_ctx(args=["work"])))
        assert upd.message.replies == ["Session: work"]
        assert runner.current_name == "work"

    def test_invalid_name_rejected(self, runner):
        upd = make_update()
        asyncio.run(m.cmd_session(upd, make_ctx(args=["NOT/valid!"])))
        assert "Invalid name" in upd.message.replies[0]
        assert runner.current_name == "main"

    def test_list_shows_buttons_with_current_marker(self, runner):
        asyncio.run(runner.switch(1, "work"))
        upd = make_update("/session")
        asyncio.run(m.cmd_session(upd, make_ctx()))
        markup = upd.message.reply_markups[0]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "• work" in labels
        assert "main" in labels

    def test_del_subcommand(self, runner):
        asyncio.run(runner.switch(1, "tmp"))
        upd = make_update()
        asyncio.run(m.cmd_session(upd, make_ctx(args=["del", "tmp"])))
        assert "deleted" in upd.message.replies[0]
        assert runner.current_name == "main"

    def test_foreign_user_ignored(self, runner):
        upd = make_update(uid=999)
        asyncio.run(m.cmd_session(upd, make_ctx(args=["work"])))
        assert upd.message.replies == []
        assert runner.current_name == "main"


# ---------------------------------------------------------------------------
# model picker
# ---------------------------------------------------------------------------

class TestModelPicker:
    def test_choices_are_label_value_pairs(self):
        assert all(len(c) == 2 for c in m.MODEL_CHOICES)
        assert "default" in m.MODEL_VALUES
        assert "opus" in m.MODEL_VALUES

    def test_free_text_model_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "MODEL_FILE", tmp_path / "model.json")
        upd = make_update()
        asyncio.run(m.cmd_model(upd, make_ctx(args=["claude-sonnet-5"])))
        assert "Model set to claude-sonnet-5" in upd.message.replies[0]
        assert m.load_model() == "claude-sonnet-5"

    def test_invalid_free_text_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "MODEL_FILE", tmp_path / "model.json")
        upd = make_update()
        asyncio.run(m.cmd_model(upd, make_ctx(args=["bad name!!"])))
        assert "Invalid model name" in upd.message.replies[0]
        assert m.load_model() == "default"

    def test_keyboard_uses_values_and_marks_current(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "MODEL_FILE", tmp_path / "model.json")
        m.save_model("opus")
        upd = make_update("/model")
        asyncio.run(m.cmd_model(upd, make_ctx()))
        markup = upd.message.reply_markups[0]
        buttons = [b for row in markup.inline_keyboard for b in row]
        by_data = {b.callback_data: b.text for b in buttons}
        assert by_data["model:opus"] == "• Opus"
        assert by_data["model:claude-sonnet-5"] == "Sonnet 5"


# ---------------------------------------------------------------------------
# scheduler: timezone + per-job model
# ---------------------------------------------------------------------------

class TestSchedulerTz:
    def test_default_timezone_is_rome(self, monkeypatch):
        monkeypatch.delenv("TGCR_TZ", raising=False)
        monkeypatch.delenv("TZ", raising=False)
        assert sched_mod._sched_tz() == ZoneInfo("Europe/Rome")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TGCR_TZ", "America/New_York")
        assert sched_mod._sched_tz() == ZoneInfo("America/New_York")

    def test_bad_tz_falls_back_to_utc(self, monkeypatch):
        monkeypatch.setenv("TGCR_TZ", "Not/AZone")
        from datetime import timezone as tz
        assert sched_mod._sched_tz() == tz.utc


class TestSchedulerModelRouting:
    class Recorder:
        def __init__(self):
            self.calls = []

        async def __call__(self, prompt, chat_id, uid, model=None):
            self.calls.append((prompt, chat_id, uid, model))

    class FakeJQ:
        def __init__(self):
            self.jobs = []

        def run_once(self, cb, when, data=None, name=None):
            self.jobs.append((cb, when, data, name))

        def get_jobs_by_name(self, name):
            return []

    def test_add_stores_model_and_fire_passes_it(self, tmp_path):
        rec = self.Recorder()
        s = sched_mod.Scheduler(tmp_path / "jobs.json", self.FakeJQ(), rec)
        job_id = s.add("* * * * *", "ciao", 1, 2, model="haiku")
        assert s.list()[0]["model"] == "haiku"
        ctx = SimpleNamespace(job=SimpleNamespace(data=job_id))
        asyncio.run(s._fire(ctx))
        assert rec.calls == [("ciao", 1, 2, "haiku")]

    def test_model_defaults_to_none(self, tmp_path):
        s = sched_mod.Scheduler(tmp_path / "jobs.json", self.FakeJQ(), self.Recorder())
        s.add("* * * * *", "ciao", 1, 2)
        assert s.list()[0]["model"] is None


class TestScheduleModelParsing:
    @pytest.fixture
    def sched(self, monkeypatch, tmp_path):
        rec = TestSchedulerModelRouting.Recorder()
        s = sched_mod.Scheduler(
            tmp_path / "jobs.json", TestSchedulerModelRouting.FakeJQ(), rec
        )
        monkeypatch.setattr(m, "scheduler", s)
        return s

    def test_model_token_parsed(self, sched):
        upd = make_update('/schedule add "0 9 * * *" model=haiku fai il report')
        args = ["add", '"0', '9', '*', '*', '*"', 'model=haiku', 'fai']
        asyncio.run(m.cmd_schedule(upd, make_ctx(args=args)))
        job = sched.list()[0]
        assert job["model"] == "haiku"
        assert job["prompt"] == "fai il report"
        assert "(model haiku)" in upd.message.replies[0]

    def test_without_model_token(self, sched):
        upd = make_update('/schedule add "0 9 * * *" fai il report')
        asyncio.run(m.cmd_schedule(upd, make_ctx(args=["add", "x"])))
        job = sched.list()[0]
        assert job["model"] is None
        assert job["prompt"] == "fai il report"

    def test_model_without_prompt_is_prompt(self, sched):
        # "model=haiku" alone (no prompt after) is treated as the prompt itself
        upd = make_update('/schedule add "0 9 * * *" model=haiku')
        asyncio.run(m.cmd_schedule(upd, make_ctx(args=["add", "x"])))
        job = sched.list()[0]
        assert job["model"] is None
        assert job["prompt"] == "model=haiku"


# ---------------------------------------------------------------------------
# proactive notify inbox
# ---------------------------------------------------------------------------

class TestNotifyInbox:
    @pytest.fixture
    def notify_env(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        (ws / ".notify").mkdir(parents=True)
        monkeypatch.setattr(m, "WORKSPACE_DIR", ws)
        monkeypatch.setattr(m, "CHAT_FILE", tmp_path / "chat.json")
        return ws

    def test_delivers_and_deletes_in_mtime_order(self, notify_env):
        import os, time
        m._save_chat_id(42)
        n = notify_env / ".notify"
        first = n / "a.txt"
        second = n / "b.md"
        first.write_text("primo")
        second.write_text("secondo")
        now = time.time()
        os.utime(first, (now - 10, now - 10))
        os.utime(second, (now, now))
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        texts = [t for _, t, _ in bot.sent]
        assert texts == ["🔔 primo", "🔔 secondo"]
        assert list(n.iterdir()) == []

    def test_no_chat_id_keeps_files(self, notify_env):
        (notify_env / ".notify" / "x.txt").write_text("ciao")
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        assert bot.sent == []
        assert (notify_env / ".notify" / "x.txt").exists()

    def test_failed_and_hidden_files_skipped(self, notify_env):
        m._save_chat_id(42)
        n = notify_env / ".notify"
        (n / "old.txt.failed").write_text("no")
        (n / ".hidden").write_text("no")
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        assert bot.sent == []
        assert (n / "old.txt.failed").exists()

    def test_send_failure_renames_to_failed(self, notify_env):
        m._save_chat_id(42)
        n = notify_env / ".notify"
        (n / "boom.txt").write_text("ciao")

        class BadBot:
            async def send_message(self, chat_id, text, **kw):
                raise RuntimeError("no network")

        asyncio.run(m.check_notify(SimpleNamespace(bot=BadBot())))
        assert not (n / "boom.txt").exists()
        assert (n / "boom.txt.failed").exists()

    def test_missing_dir_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "WORKSPACE_DIR", tmp_path / "nope")
        bot = FakeBot()
        asyncio.run(m.check_notify(SimpleNamespace(bot=bot)))
        assert bot.sent == []


class TestChatIdPersistence:
    def test_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(m, "CHAT_FILE", tmp_path / "chat.json")
        assert m._load_chat_id() is None
        m._save_chat_id(42)
        assert m._load_chat_id() == 42
        m._save_chat_id(42)  # idempotent
        assert json.loads((tmp_path / "chat.json").read_text()) == {"chat_id": 42}

    def test_corrupt_file_returns_none(self, monkeypatch, tmp_path):
        f = tmp_path / "chat.json"
        f.write_text("not json")
        monkeypatch.setattr(m, "CHAT_FILE", f)
        assert m._load_chat_id() is None


# ---------------------------------------------------------------------------
# system prompt appendix
# ---------------------------------------------------------------------------

def test_system_appendix_mentions_notify_and_tgq():
    assert "TGQUESTION" in m._SYSTEM_APPENDIX
    assert ".notify" in m._SYSTEM_APPENDIX


# ---------------------------------------------------------------------------
# copy buttons for code blocks
# ---------------------------------------------------------------------------

class TestCopySnippets:
    def test_extracts_fenced_blocks(self):
        text = "Esegui:\n```bash\ndocker compose up -d\n```\npoi:\n```\ngit pull\n```"
        assert m._copy_snippets(text) == ["docker compose up -d", "git pull"]

    def test_skips_blocks_over_256_chars(self):
        long_block = "x" * 300
        text = f"```\n{long_block}\n```\n```\nok\n```"
        assert m._copy_snippets(text) == ["ok"]

    def test_dedupes_and_caps_at_four(self):
        blocks = "\n".join(f"```\ncmd{i}\n```" for i in [1, 1, 2, 3, 4, 5])
        snippets = m._copy_snippets(blocks)
        assert snippets == ["cmd1", "cmd2", "cmd3", "cmd4"]

    def test_no_blocks_no_snippets(self):
        assert m._copy_snippets("solo testo, `inline` non conta") == []

    def test_keyboard_labels_and_payload(self):
        kb = m._copy_keyboard("```bash\ndocker compose up -d --build && echo fatto ciao\n```")
        [row] = kb.inline_keyboard
        btn = row[0]
        assert btn.text.startswith("📋 docker compose up -d --bui")
        assert btn.text.endswith("…")
        assert btn.copy_text.text == "docker compose up -d --build && echo fatto ciao"

    def test_keyboard_none_without_blocks(self):
        assert m._copy_keyboard("niente codice qui") is None


class TestSendMdCopyButtons:
    def test_markup_only_on_last_chunk(self):
        long_text = ("riga di testo normale\n" * 200) + "```\ngit status\n```"
        bot = FakeBot()
        asyncio.run(m.send_md(bot, 1, long_text))
        assert len(bot.sent) > 1
        markups = [kw.get("reply_markup") for _, _, kw in bot.sent]
        assert all(mk is None for mk in markups[:-1])
        last = markups[-1]
        assert last is not None
        assert last.inline_keyboard[0][0].copy_text.text == "git status"

    def test_no_markup_without_code(self):
        bot = FakeBot()
        asyncio.run(m.send_md(bot, 1, "ciao"))
        assert bot.sent[0][2].get("reply_markup") is None
