"""Persistent recurring job scheduler backed by python-telegram-bot's JobQueue."""

import json
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Awaitable, Callable

from croniter import croniter
from telegram.ext import ContextTypes, JobQueue

log = logging.getLogger(__name__)

Runner = Callable[[str, int, int], Awaitable[None]]


class Scheduler:
    def __init__(self, store_path: Path, job_queue: JobQueue, runner: Runner):
        self._store = store_path
        self._jq = job_queue
        self._runner = runner
        self._jobs: list[dict] = []

    def load(self) -> None:
        if self._store.exists():
            try:
                self._jobs = json.loads(self._store.read_text())
            except Exception as e:
                log.error("Failed to load %s: %s", self._store, e)
                self._jobs = []
        for job in self._jobs:
            self._schedule_next(job)

    def _save(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        self._store.write_text(json.dumps(self._jobs, indent=2))

    def _schedule_next(self, job: dict) -> None:
        try:
            now = datetime.now(timezone.utc)
            it = croniter(job["cron"], now)
            next_time = it.get_next(datetime)
            delay = max(1, int((next_time - now).total_seconds()))
        except Exception as e:
            log.error("Bad cron %r for job %s: %s", job["cron"], job["id"], e)
            return
        self._jq.run_once(
            self._fire,
            when=delay,
            data=job["id"],
            name=f"sched-{job['id']}",
        )
        log.info("Job %s scheduled in %ds (cron=%s)", job["id"], delay, job["cron"])

    async def _fire(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        job_id = ctx.job.data
        job = next((j for j in self._jobs if j["id"] == job_id), None)
        if not job:
            return
        try:
            await self._runner(job["prompt"], job["chat_id"], job["uid"])
        except Exception as e:
            log.exception("Scheduled job %s failed: %s", job_id, e)
        self._schedule_next(job)

    def add(self, cron: str, prompt: str, chat_id: int, uid: int) -> str:
        try:
            croniter(cron)
        except Exception as e:
            raise ValueError(str(e))
        job_id = uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "cron": cron,
            "prompt": prompt,
            "chat_id": chat_id,
            "uid": uid,
            "created": int(time.time()),
        }
        self._jobs.append(job)
        self._save()
        self._schedule_next(job)
        return job_id

    def remove(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j["id"] != job_id]
        if len(self._jobs) == before:
            return False
        self._save()
        for j in self._jq.get_jobs_by_name(f"sched-{job_id}"):
            j.schedule_removal()
        return True

    def list(self) -> list[dict]:
        return list(self._jobs)
