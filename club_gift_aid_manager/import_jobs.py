"""A small, bounded background queue for CSV imports.

Web server threads should return quickly instead of waiting while hundreds of
routes are calculated or database rows are validated.  This module owns a
fixed-size thread pool and a bounded number of waiting jobs.  The bound is an
important availability control: once the queue is full, callers receive a
clear 503 response instead of consuming unlimited memory.

The queue intentionally stores only short-lived status information in memory.
Production deployments that run several worker processes should replace this
module with a shared durable queue, but the API around ``submit_import`` and
``get_import`` can remain the same.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
import logging
from threading import BoundedSemaphore, Lock
from typing import Any, Callable


MAX_WORKERS = 2
MAX_QUEUED_JOBS = 8
JOB_RETENTION = timedelta(hours=1)


class ImportQueueFull(Exception):
    """Raised when accepting another job would exceed the safe queue bound."""


@dataclass
class ImportJob:
    """Mutable state for one import, protected by the module-level lock."""

    id: str
    owner_id: int
    kind: str
    status: str = "queued"
    result: dict[str, Any] | None = None
    http_status: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="csv-import")
_capacity = BoundedSemaphore(MAX_WORKERS + MAX_QUEUED_JOBS)
_lock = Lock()
_jobs: dict[str, ImportJob] = {}
_logger = logging.getLogger(__name__)


def _remove_expired_jobs(now: datetime) -> None:
    """Discard completed status records after callers have had time to poll."""
    expired = [
        job_id for job_id, job in _jobs.items()
        if job.finished_at is not None and now - job.finished_at > JOB_RETENTION
    ]
    for job_id in expired:
        del _jobs[job_id]


def submit_import(
    owner_id: int,
    kind: str,
    work: Callable[[], tuple[dict[str, Any], int]],
) -> ImportJob:
    """Submit work without blocking, or reject it when the queue is full."""
    if not _capacity.acquire(blocking=False):
        raise ImportQueueFull

    job = ImportJob(id=secrets.token_urlsafe(18), owner_id=owner_id, kind=kind)
    with _lock:
        _remove_expired_jobs(datetime.now(timezone.utc))
        _jobs[job.id] = job

    def run() -> None:
        try:
            with _lock:
                job.status = "running"
            result, status = work()
            with _lock:
                job.result = result
                job.http_status = status
                job.status = "completed" if status < 400 else "failed"
        except Exception:
            # Do not expose exception details, filesystem paths, or secrets to
            # the polling client.  Server logging can still capture failures.
            _logger.exception("Background %s import %s failed", kind, job.id)
            with _lock:
                job.result = {"error": "The import could not be completed."}
                job.http_status = 500
                job.status = "failed"
        finally:
            with _lock:
                job.finished_at = datetime.now(timezone.utc)
            _capacity.release()

    _executor.submit(run)
    return job


def get_import(job_id: str, owner_id: int) -> ImportJob | None:
    """Return a job only to the account that originally submitted it."""
    with _lock:
        _remove_expired_jobs(datetime.now(timezone.utc))
        job = _jobs.get(job_id)
        return job if job is not None and job.owner_id == owner_id else None


def serialise_import(job: ImportJob) -> dict[str, Any]:
    """Create the stable JSON representation returned by the polling route."""
    payload: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
    }
    if job.result is not None:
        payload["result"] = job.result
        payload["http_status"] = job.http_status
    return payload
