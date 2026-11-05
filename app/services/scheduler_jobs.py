"""Small scheduling primitives kept independent of Redis/RQ implementations."""
from __future__ import annotations

from uuid import uuid4


def enqueue_daily_evidence_integrity_audit(redis, queue, *, enabled: bool = True) -> str | None:
    """Enqueue one daily scan, using a Redis lease to prevent duplicate schedulers."""
    if not enabled:
        return None

    lock = redis.lock(
        "cignal:scheduler:evidence-integrity",
        timeout=23 * 60 * 60,
        blocking_timeout=1,
    )
    if not lock.acquire(blocking=False):
        return None

    run_id = str(uuid4())
    try:
        queue.enqueue(
            "app.worker.run_evidence_integrity_audit",
            run_id,
            job_timeout=6 * 60 * 60,
            result_ttl=86400,
        )
    except Exception:
        lock.release()
        raise
    return run_id
