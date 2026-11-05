"""Durable execution leases, attempt history and crash recovery for RQ jobs."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_

from app.db.models import (
    BackgroundJobExecutionAttempt,
    BackgroundJobOutbox,
    utcnow,
)
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_ACTIVE_RQ_STATES = {"queued", "started", "deferred", "scheduled"}
_TERMINAL_RQ_FAILURE_STATES = {"failed", "stopped", "canceled", "cancelled"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _status_name(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw).lower().split(".")[-1]


def _queue_for(queue_registry, queue_name: str):
    if isinstance(queue_registry, dict):
        return queue_registry.get(queue_name)
    return queue_registry


def claim_execution(
    outbox_id: str,
    *,
    rq_job_id: str,
    worker_name: str,
    session_factory=SessionLocal,
    lease_seconds: int = 2100,
    max_attempts: int = 8,
    now: datetime | None = None,
) -> dict:
    """Claim one logical job execution; a live lease suppresses duplicate workers."""
    current = _aware(now or utcnow())
    with session_factory() as db:
        row = (
            db.query(BackgroundJobOutbox)
            .filter(BackgroundJobOutbox.id == outbox_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            return {"status": "not_found"}
        if row.status == "completed" or row.completed_at is not None:
            return {"status": "already_completed", "job_id": row.job_id}
        if row.status == "dead":
            return {"status": "dead", "job_id": row.job_id}
        # A queued RQ delivery is fenced to the exact delivery ID recorded by
        # the outbox relay. A failed/expired delivery is retired before retry;
        # stale jobs manually requeued from RQ must never claim a later attempt.
        if not rq_job_id or row.last_rq_job_id != str(rq_job_id):
            return {"status": "stale_delivery", "job_id": row.job_id}
        current_lease = _aware(row.execution_lease_expires_at)
        if row.status == "running" and current_lease and current_lease > current:
            return {"status": "already_running", "job_id": row.job_id}
        if row.status not in {"pending", "publishing", "enqueued", "running"}:
            return {"status": "not_dispatchable", "job_id": row.job_id}
        if int(row.execution_attempts or 0) >= max(1, int(max_attempts)):
            row.status = "dead"
            row.last_execution_error_code = row.last_execution_error_code or "execution_attempt_budget_exhausted"
            row.last_error_code = row.last_execution_error_code
            row.updated_at = current
            db.commit()
            return {"status": "dead", "job_id": row.job_id}

        attempt_number = int(row.execution_attempts or 0) + 1
        token = str(uuid4())
        lease_expiry = current + timedelta(seconds=max(30, int(lease_seconds)))
        attempt = BackgroundJobExecutionAttempt(
            id=str(uuid4()),
            outbox_id=row.id,
            generation=int(row.execution_generation or 0),
            attempt_number=attempt_number,
            rq_job_id=str(rq_job_id)[:80],
            worker_name=str(worker_name or "unknown")[:160],
            queue_name=row.queue_name,
            status="running",
            started_at=current,
            lease_expires_at=lease_expiry,
        )
        row.execution_attempts = attempt_number
        row.execution_lease_token = token
        row.execution_lease_expires_at = lease_expiry
        row.last_started_at = current
        row.status = "running"
        row.updated_at = current
        db.add(attempt)
        db.commit()
        return {
            "status": "claimed",
            "outbox_id": row.id,
            "job_id": row.job_id,
            "function_path": row.function_path,
            "task_args": list(row.task_args or []),
            "queue_name": row.queue_name,
            "execution_generation": int(row.execution_generation or 0),
            "attempt_number": attempt_number,
            "attempt_id": attempt.id,
            "lease_token": token,
            "lease_expires_at": lease_expiry.isoformat(),
        }


def complete_execution(
    outbox_id: str,
    attempt_id: str,
    lease_token: str,
    *,
    result_sha256: str | None = None,
    session_factory=SessionLocal,
    now: datetime | None = None,
) -> bool:
    current = _aware(now or utcnow())
    with session_factory() as db:
        row = (
            db.query(BackgroundJobOutbox)
            .filter(BackgroundJobOutbox.id == outbox_id)
            .with_for_update()
            .one_or_none()
        )
        attempt = db.get(BackgroundJobExecutionAttempt, attempt_id)
        if (
            row is None or attempt is None
            or row.execution_lease_token != lease_token
            or attempt.outbox_id != outbox_id
            or attempt.status != "running"
        ):
            db.rollback()
            return False
        row.status = "completed"
        row.completed_at = current
        row.execution_lease_token = None
        row.execution_lease_expires_at = None
        row.lease_token = None
        row.lease_expires_at = None
        row.enqueued_at = row.enqueued_at or current
        row.last_execution_error_code = None
        row.last_error_code = None
        row.updated_at = current
        attempt.status = "completed"
        attempt.completed_at = current
        attempt.result_sha256 = result_sha256
        db.commit()
        return True


def fail_execution(
    outbox_id: str,
    attempt_id: str,
    lease_token: str,
    error: Exception | str,
    *,
    session_factory=SessionLocal,
    max_attempts: int = 8,
    base_delay_seconds: int = 10,
    max_delay_seconds: int = 3600,
    now: datetime | None = None,
) -> str:
    current = _aware(now or utcnow())
    error_code = (type(error).__name__ if isinstance(error, Exception) else str(error))[:80]
    with session_factory() as db:
        row = (
            db.query(BackgroundJobOutbox)
            .filter(BackgroundJobOutbox.id == outbox_id)
            .with_for_update()
            .one_or_none()
        )
        attempt = db.get(BackgroundJobExecutionAttempt, attempt_id)
        if (
            row is None or attempt is None
            or row.execution_lease_token != lease_token
            or attempt.outbox_id != outbox_id
            or attempt.status != "running"
        ):
            db.rollback()
            return "lease_lost"
        attempt.status = "failed"
        attempt.completed_at = current
        attempt.error_code = error_code
        row.execution_lease_token = None
        row.execution_lease_expires_at = None
        row.lease_token = None
        row.lease_expires_at = None
        row.last_execution_error_code = error_code
        row.last_error_code = error_code
        row.updated_at = current
        if int(row.execution_attempts or 0) >= max(1, int(max_attempts)):
            row.status = "dead"
            outcome = "dead"
        else:
            exponent = min(max(0, int(row.execution_attempts or 1) - 1), 9)
            delay = min(max(1, int(max_delay_seconds)), max(1, int(base_delay_seconds)) * (2 ** exponent))
            row.status = "pending"
            row.last_rq_job_id = None  # retire the failed delivery before publishing a replacement
            row.available_at = current + timedelta(seconds=delay)
            outcome = "retried"
        db.commit()
        return outcome


def recover_expired_executions(
    *,
    session_factory=SessionLocal,
    batch_size: int = 100,
    max_attempts: int = 8,
    now: datetime | None = None,
) -> dict[str, int]:
    """Recover worker crashes after the execution lease has expired."""
    current = _aware(now or utcnow())
    result = {"checked": 0, "requeued": 0, "dead": 0}
    with session_factory() as db:
        rows = (
            db.query(BackgroundJobOutbox)
            .filter(
                BackgroundJobOutbox.status == "running",
                BackgroundJobOutbox.execution_lease_expires_at.is_not(None),
                BackgroundJobOutbox.execution_lease_expires_at <= current,
            )
            .order_by(BackgroundJobOutbox.execution_lease_expires_at.asc())
            .limit(max(1, min(500, int(batch_size))))
            .with_for_update(skip_locked=True)
            .all()
        )
        for row in rows:
            result["checked"] += 1
            attempt = (
                db.query(BackgroundJobExecutionAttempt)
                .filter(
                    BackgroundJobExecutionAttempt.outbox_id == row.id,
                    BackgroundJobExecutionAttempt.generation == int(row.execution_generation or 0),
                    BackgroundJobExecutionAttempt.attempt_number == int(row.execution_attempts or 0),
                    BackgroundJobExecutionAttempt.status == "running",
                )
                .one_or_none()
            )
            if attempt is not None:
                attempt.status = "abandoned"
                attempt.completed_at = current
                attempt.error_code = "execution_lease_expired"
            row.execution_lease_token = None
            row.execution_lease_expires_at = None
            row.lease_token = None
            row.lease_expires_at = None
            row.last_execution_error_code = "execution_lease_expired"
            row.last_error_code = "execution_lease_expired"
            row.updated_at = current
            if int(row.execution_attempts or 0) >= max(1, int(max_attempts)):
                row.status = "dead"
                result["dead"] += 1
            else:
                row.status = "pending"
                row.last_rq_job_id = None  # old delivery is fenced from the next attempt
                row.available_at = current
                result["requeued"] += 1
        db.commit()
    return result


def reconcile_enqueued_jobs(
    queue_registry,
    *,
    session_factory=SessionLocal,
    batch_size: int = 200,
    grace_seconds: int = 120,
    lease_grace_seconds: int = 120,
    max_dispatch_attempts: int = 12,
    now: datetime | None = None,
) -> dict[str, int]:
    """Reconcile stale RQ deliveries and adopt pre-upgrade publisher leases.

    Legacy Phase 5 publishers used the logical outbox ID as the RQ ID. Migration
    0021 backfills that pointer for rows left in ``publishing`` as well as
    ``enqueued``. Such an expired publisher lease must be inspected in Redis
    before a replacement delivery is created.
    """
    current = _aware(now or utcnow())
    result = {"checked": 0, "waiting": 0, "completed": 0, "requeued": 0, "dead": 0, "queue_unavailable": 0}
    stale_enqueued = and_(
        BackgroundJobOutbox.status == "enqueued",
        BackgroundJobOutbox.enqueued_at.is_not(None),
        BackgroundJobOutbox.enqueued_at <= current - timedelta(seconds=max(15, int(grace_seconds))),
    )
    expired_publishing = and_(
        BackgroundJobOutbox.status == "publishing",
        BackgroundJobOutbox.last_rq_job_id.is_not(None),
        or_(
            BackgroundJobOutbox.lease_expires_at.is_(None),
            BackgroundJobOutbox.lease_expires_at <= current,
        ),
    )
    with session_factory() as db:
        rows = (
            db.query(BackgroundJobOutbox)
            .filter(or_(stale_enqueued, expired_publishing))
            .order_by(BackgroundJobOutbox.updated_at.asc())
            .limit(max(1, min(1000, int(batch_size))))
            .with_for_update(skip_locked=True)
            .all()
        )
        for row in rows:
            was_publishing = row.status == "publishing"
            if not was_publishing:
                age = (current - _aware(row.enqueued_at)).total_seconds()
                required_age = max(
                    max(15, int(grace_seconds)),
                    int(row.job_timeout_seconds or 300) + max(30, int(lease_grace_seconds)),
                )
                if age < required_age:
                    continue
            result["checked"] += 1
            queue = _queue_for(queue_registry, row.queue_name)
            if queue is None:
                result["queue_unavailable"] += 1
                continue
            rq_job_id = row.last_rq_job_id or row.job_id
            try:
                job = queue.fetch_job(rq_job_id)
                state = _status_name(job.get_status(refresh=True)) if job is not None else "missing"
            except Exception:
                result["queue_unavailable"] += 1
                logger.exception("Could not inspect stale RQ job %s", rq_job_id)
                continue

            if state in _ACTIVE_RQ_STATES:
                # Adopt a legacy publisher's accepted Redis write. Started jobs
                # are never speculatively replaced: RQ's hard timeout or execution
                # lease recovery will eventually make their failure observable.
                if was_publishing:
                    row.status = "enqueued"
                    row.enqueued_at = current
                    row.lease_token = None
                    row.lease_expires_at = None
                    row.updated_at = current
                result["waiting"] += 1
                continue
            if state == "finished":
                row.status = "completed"
                row.completed_at = current
                row.execution_lease_token = None
                row.execution_lease_expires_at = None
                row.lease_token = None
                row.lease_expires_at = None
                row.last_error_code = None
                row.last_execution_error_code = None
                row.updated_at = current
                result["completed"] += 1
                continue

            outcome_code = f"rq_{state}"
            row.last_error_code = outcome_code
            row.last_execution_error_code = outcome_code
            row.lease_token = None
            row.lease_expires_at = None
            row.updated_at = current
            if int(row.attempts or 0) >= max(1, int(max_dispatch_attempts)):
                row.status = "dead"
                result["dead"] += 1
            else:
                row.status = "pending"
                row.last_rq_job_id = None  # failed/missing RQ delivery is no longer claimable
                row.available_at = current + timedelta(seconds=5)
                result["requeued"] += 1
        db.commit()
    return result
