"""
In-process job store for the Scribe extension service.

Every pipeline call this service wraps is blocking (sync openai client,
time.sleep() rate limiting in abstract_fetcher.enrich_entries) and some
have multi-minute worst-case tails (see pipeline/evaluator.py's
evaluate_evidence_support docstring). A single local user, no persistence
needed across restarts — an in-memory dict plus a small thread pool is the
right amount of machinery here, not a task queue.
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

_executor = ThreadPoolExecutor(max_workers=4)
_jobs: dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    status: str = "pending"  # pending | running | done | error
    progress: dict[str, Any] = field(default_factory=lambda: {"stage": "queued", "done": 0, "total": 0})
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


def create_job(fn: Callable[["Job"], None]) -> Job:
    """
    Create a job and start it in the thread pool immediately. `fn`
    receives the Job object and is responsible for updating
    job.progress as it goes and setting job.result (on success) or
    job.error (on failure) before returning.
    """
    job = Job(id=uuid.uuid4().hex)
    _jobs[job.id] = job

    def run():
        job.status = "running"
        try:
            fn(job)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
            job.status = "error"
            job.error = str(exc)

    _executor.submit(run)
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)
