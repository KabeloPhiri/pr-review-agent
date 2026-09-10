"""Background review jobs.

Databricks Apps cut HTTP connections at roughly 120 seconds, so a large pull
request cannot be reviewed inside the request that asked for it. Async mode
returns a job id immediately and posts comments when the work finishes.

`InMemoryJobStore` is the default: job state lives in this process, which
means it is lost on restart and is not shared across replicas. That is fine
for fire-and-forget reviewing (the comments land on the PR either way) and is
only a limitation for a pipeline that polls for a gate — see the README.
Swapping in a Lakebase-backed store is a new class implementing `JobStore`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from enum import Enum
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel, Field

from app.core.models import ReviewResult

logger = logging.getLogger(__name__)

#: How long a finished job stays readable before it is evicted.
JOB_TTL_SECONDS = 3600
MAX_JOBS = 500


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    pr: str = ""
    result: ReviewResult | None = None
    error: str | None = None

    def touch(self, status: JobStatus) -> None:
        self.status = status
        self.updated_at = time.time()


class JobStore(Protocol):
    async def create(self, pr: str) -> Job: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def save(self, job: Job) -> None: ...


class InMemoryJobStore:
    def __init__(self, ttl_seconds: int = JOB_TTL_SECONDS, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds
        self._max_jobs = max_jobs

    async def create(self, pr: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex, pr=pr)
        async with self._lock:
            self._evict()
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def save(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job

    def _evict(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.updated_at < cutoff
            and job.status in {JobStatus.COMPLETED, JobStatus.FAILED}
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)
        while len(self._jobs) >= self._max_jobs:
            oldest = min(self._jobs.values(), key=lambda j: j.updated_at)
            self._jobs.pop(oldest.job_id, None)


class JobRunner:
    """Runs review coroutines in the background and records the outcome."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    async def submit(self, pr: str, work: Callable[[], Awaitable[ReviewResult]]) -> Job:
        job = await self.store.create(pr)
        task = asyncio.create_task(self._run(job, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, work: Callable[[], Awaitable[ReviewResult]]) -> None:
        job.touch(JobStatus.RUNNING)
        await self.store.save(job)
        try:
            job.result = await work()
            job.touch(JobStatus.COMPLETED)
        except Exception as exc:
            logger.exception("Review job %s failed for %s", job.job_id, job.pr)
            job.error = f"{type(exc).__name__}: {exc}"
            job.touch(JobStatus.FAILED)
        await self.store.save(job)

    async def wait_for(self, job_id: str, timeout: float, interval: float = 0.5) -> Job | None:
        """Poll until the job reaches a terminal state or `timeout` elapses.

        Polling (rather than awaiting the task) keeps this correct for any
        `JobStore`, including a future shared one.
        """
        deadline = time.monotonic() + timeout
        while True:
            job = await self.store.get(job_id)
            if job is None or job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                return job
            if time.monotonic() >= deadline:
                return job
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    async def drain(self, timeout: float = 30.0) -> None:
        """Give running reviews a chance to finish on shutdown."""
        if not self._tasks:
            return
        await asyncio.wait(set(self._tasks), timeout=timeout)
