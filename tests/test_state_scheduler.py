"""Tests for bot/session_state.py and bot/scheduler.py."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import session_state
from bot.scheduler import Scheduler


# ---------------------------------------------------------------------------
# session_state helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def state_paths(tmp_path, monkeypatch):
    """Point STATE_DIR/STATE_FILE at a fresh tmp location for each test."""
    state_dir = tmp_path / "state"
    state_file = state_dir / "state.json"
    monkeypatch.setattr(session_state, "STATE_DIR", state_dir)
    monkeypatch.setattr(session_state, "STATE_FILE", state_file)
    return state_dir, state_file


# -- _atomic_write ----------------------------------------------------------


def test_atomic_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "out.json"
    session_state._atomic_write(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("old-content")
    session_state._atomic_write(target, "new-content")
    assert target.read_text() == "new-content"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.json"
    session_state._atomic_write(target, "data")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_atomic_write_failure_cleans_up_and_preserves_original(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("original")
    # f.write(123) raises TypeError; the original file must survive and the
    # temp file must be unlinked.
    with pytest.raises(TypeError):
        session_state._atomic_write(target, 123)
    assert target.read_text() == "original"
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


# -- load_state / save_state / clear_state ----------------------------------


def test_load_state_missing_file_returns_empty_dict(state_paths):
    assert session_state.load_state() == {}


def test_load_state_corrupt_json_returns_empty_dict(state_paths):
    state_dir, state_file = state_paths
    state_dir.mkdir(parents=True)
    state_file.write_text("{not valid json!!")
    assert session_state.load_state() == {}


def test_save_load_roundtrip(state_paths):
    _, state_file = state_paths
    data = {"session_id": "abc123", "cwd": "/tmp/ws", "model": "opus"}
    session_state.save_state(data)
    assert state_file.exists()
    assert session_state.load_state() == data
    # on-disk representation is real JSON
    assert json.loads(state_file.read_text()) == data


def test_save_state_overwrites_previous(state_paths):
    session_state.save_state({"session_id": "first"})
    session_state.save_state({"session_id": "second"})
    assert session_state.load_state() == {"session_id": "second"}


def test_clear_state_removes_file(state_paths):
    _, state_file = state_paths
    session_state.save_state({"x": 1})
    assert state_file.exists()
    session_state.clear_state()
    assert not state_file.exists()
    assert session_state.load_state() == {}


def test_clear_state_noop_when_missing(state_paths):
    # Must not raise when there is nothing to clear.
    session_state.clear_state()
    assert session_state.load_state() == {}


# ---------------------------------------------------------------------------
# Scheduler fakes
# ---------------------------------------------------------------------------


class FakeJob:
    def __init__(self, name):
        self.name = name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    """Records run_once calls and hands out FakeJobs by name."""

    def __init__(self):
        self.run_once_calls = []  # list of dicts: callback/when/data/name
        self.jobs_by_name = {}  # name -> list[FakeJob]

    def run_once(self, callback, when=None, data=None, name=None):
        self.run_once_calls.append(
            {"callback": callback, "when": when, "data": data, "name": name}
        )
        job = FakeJob(name)
        self.jobs_by_name.setdefault(name, []).append(job)
        return job

    def get_jobs_by_name(self, name):
        return tuple(self.jobs_by_name.get(name, []))


class RecordingRunner:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    async def __call__(self, prompt, chat_id, uid):
        self.calls.append((prompt, chat_id, uid))
        if self.exc is not None:
            raise self.exc


@pytest.fixture
def sched(tmp_path):
    jq = FakeJobQueue()
    runner = RecordingRunner()
    store = tmp_path / "sched" / "jobs.json"
    s = Scheduler(store, jq, runner)
    return SimpleNamespace(s=s, jq=jq, runner=runner, store=store)


def make_ctx(job_id):
    return SimpleNamespace(job=SimpleNamespace(data=job_id))


# -- add --------------------------------------------------------------------


def test_add_valid_cron_persists_and_schedules(sched):
    job_id = sched.s.add("*/5 * * * *", "do stuff", 42, 7)
    assert isinstance(job_id, str) and len(job_id) == 8

    # persisted to the store file (parent dir created on demand)
    stored = json.loads(sched.store.read_text())
    assert len(stored) == 1
    job = stored[0]
    assert job["id"] == job_id
    assert job["cron"] == "*/5 * * * *"
    assert job["prompt"] == "do stuff"
    assert job["chat_id"] == 42
    assert job["uid"] == 7
    assert isinstance(job["created"], int)

    # scheduled on the job queue
    assert len(sched.jq.run_once_calls) == 1
    call = sched.jq.run_once_calls[0]
    assert call["name"] == f"sched-{job_id}"
    assert call["data"] == job_id
    assert call["callback"] == sched.s._fire
    # */5 cron fires within 5 minutes; delay clamped to >= 1
    assert 1 <= call["when"] <= 300


def test_add_invalid_cron_raises_valueerror(sched):
    with pytest.raises(ValueError):
        sched.s.add("not a cron", "prompt", 1, 1)
    # nothing persisted, nothing scheduled
    assert not sched.store.exists()
    assert sched.s.list() == []
    assert sched.jq.run_once_calls == []


def test_add_multiple_jobs_all_persisted(sched):
    id1 = sched.s.add("* * * * *", "one", 1, 1)
    id2 = sched.s.add("0 0 * * *", "two", 2, 2)
    assert id1 != id2
    stored = json.loads(sched.store.read_text())
    assert [j["id"] for j in stored] == [id1, id2]
    assert len(sched.jq.run_once_calls) == 2


# -- list -------------------------------------------------------------------


def test_list_returns_jobs_copy(sched):
    assert sched.s.list() == []
    job_id = sched.s.add("* * * * *", "p", 1, 2)
    listed = sched.s.list()
    assert len(listed) == 1
    assert listed[0]["id"] == job_id
    # list() returns a copy of the container: mutating it must not affect state
    listed.clear()
    assert len(sched.s.list()) == 1


# -- remove -----------------------------------------------------------------


def test_remove_existing_returns_true_and_unschedules(sched):
    job_id = sched.s.add("* * * * *", "p", 1, 2)
    fake_jobs = sched.jq.jobs_by_name[f"sched-{job_id}"]
    assert sched.s.remove(job_id) is True
    assert sched.s.list() == []
    assert json.loads(sched.store.read_text()) == []
    assert all(j.removed for j in fake_jobs)


def test_remove_missing_returns_false(sched):
    sched.s.add("* * * * *", "p", 1, 2)
    before_store = sched.store.read_text()
    assert sched.s.remove("deadbeef") is False
    # store untouched, job still listed
    assert sched.store.read_text() == before_store
    assert len(sched.s.list()) == 1


# -- load -------------------------------------------------------------------


def test_load_reads_store_and_schedules_each(tmp_path):
    jq = FakeJobQueue()
    store = tmp_path / "jobs.json"
    jobs = [
        {"id": "aaaa1111", "cron": "* * * * *", "prompt": "a", "chat_id": 1, "uid": 1, "created": 0},
        {"id": "bbbb2222", "cron": "0 12 * * *", "prompt": "b", "chat_id": 2, "uid": 2, "created": 0},
    ]
    store.write_text(json.dumps(jobs))
    s = Scheduler(store, jq, RecordingRunner())
    s.load()
    assert [j["id"] for j in s.list()] == ["aaaa1111", "bbbb2222"]
    assert [c["name"] for c in jq.run_once_calls] == ["sched-aaaa1111", "sched-bbbb2222"]


def test_load_missing_store_is_empty_noop(sched):
    sched.s.load()
    assert sched.s.list() == []
    assert sched.jq.run_once_calls == []


def test_load_corrupt_store_results_in_empty_jobs(tmp_path, caplog):
    jq = FakeJobQueue()
    store = tmp_path / "jobs.json"
    store.write_text("[{broken json")
    s = Scheduler(store, jq, RecordingRunner())
    with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
        s.load()
    assert s.list() == []
    assert jq.run_once_calls == []
    assert any("Failed to load" in r.message for r in caplog.records)


# -- _fire ------------------------------------------------------------------


def test_fire_runs_runner_and_reschedules(sched):
    job_id = sched.s.add("* * * * *", "hello world", 99, 13)
    calls_before = len(sched.jq.run_once_calls)
    asyncio.run(sched.s._fire(make_ctx(job_id)))
    assert sched.runner.calls == [("hello world", 99, 13)]
    # one new run_once for the reschedule
    assert len(sched.jq.run_once_calls) == calls_before + 1
    assert sched.jq.run_once_calls[-1]["data"] == job_id


def test_fire_missing_job_id_is_noop(sched):
    sched.s.add("* * * * *", "p", 1, 2)
    calls_before = len(sched.jq.run_once_calls)
    asyncio.run(sched.s._fire(make_ctx("nonexistent")))
    assert sched.runner.calls == []
    assert len(sched.jq.run_once_calls) == calls_before


def test_fire_runner_exception_still_reschedules(tmp_path, caplog):
    jq = FakeJobQueue()
    runner = RecordingRunner(exc=RuntimeError("boom"))
    store = tmp_path / "jobs.json"
    s = Scheduler(store, jq, runner)
    job_id = s.add("* * * * *", "p", 5, 6)
    calls_before = len(jq.run_once_calls)
    with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
        asyncio.run(s._fire(make_ctx(job_id)))
    assert runner.calls == [("p", 5, 6)]
    assert len(jq.run_once_calls) == calls_before + 1
    assert any("failed" in r.message for r in caplog.records)


# -- _schedule_next ---------------------------------------------------------


def test_schedule_next_bad_cron_logs_and_does_not_schedule(sched, caplog):
    bad_job = {"id": "badc0de1", "cron": "totally bogus", "prompt": "p", "chat_id": 1, "uid": 1}
    with caplog.at_level(logging.ERROR, logger="bot.scheduler"):
        sched.s._schedule_next(bad_job)
    assert sched.jq.run_once_calls == []
    assert any("Bad cron" in r.message for r in caplog.records)


def test_schedule_next_delay_minimum_is_one_second(sched):
    # A fire-every-minute cron always yields a delay of at least 1 second.
    job = {"id": "cafe0001", "cron": "* * * * *", "prompt": "p", "chat_id": 1, "uid": 1}
    sched.s._schedule_next(job)
    assert len(sched.jq.run_once_calls) == 1
    assert sched.jq.run_once_calls[0]["when"] >= 1
