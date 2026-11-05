"""Transactional outbox for reliable report-analysis job dispatch.

The database commit is the source of truth. Redis/RQ availability affects only
how quickly a committed job request is delivered, not whether it is recorded.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.db.models import BackgroundJobOutbox, utcnow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_ALLOWED_FUNCTIONS = {
    "app.worker.process_report",
    "app.worker.discover_event_hypotheses",
    "app.worker.replay_event_history",
}
_ACTIVE_RQ_STATES = {"queued", "started", "deferred", "scheduled", "finished"}


def enqueue_report_analysis(db: Session, report_id: str, revision: int) -> BackgroundJobOutbox:
    """Persist a deterministic report-analysis request in the caller's transaction."""
    normalized_report_id = str(report_id).strip()
    normalized_revision = max(1, int(revision))
    if not normalized_report_id or len(normalized_report_id) > 36:
        raise ValueError("Invalid report identifier for durable analysis job")

    job_id = f"cignal-report-analysis-{normalized_report_id}-r{normalized_revision}"
    args = [normalized_report_id, normalized_revision]
    # SessionLocal disables autoflush. Check pending ORM objects first so repeated
    # calls in one parent transaction remain idempotent without forcing unrelated
    # pending report/audit rows to flush ahead of their caller's commit boundary.
    existing = next(
        (item for item in db.new if isinstance(item, BackgroundJobOutbox) and item.job_id == job_id),
        None,
    )
    if existing is None:
        existing = db.query(BackgroundJobOutbox).filter_by(job_id=job_id).one_or_none()
    if existing is not None:
        if existing.function_path != "app.worker.process_report" or existing.task_args != args:
            raise ValueError("A durable job identifier is already bound to different work")
        return existing

    row = BackgroundJobOutbox(
        id=str(uuid4()),
        job_id=job_id,
        function_path="app.worker.process_report",
        task_args=args,
        queue_name="cignal",
        job_timeout_seconds=300,
        result_ttl_seconds=3600,
        status="pending",
        attempts=0,
        available_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    return row


def enqueue_event_discovery(
    db: Session,
    run_id: str,
    parameters: dict,
) -> BackgroundJobOutbox:
    """Persist an event-discovery request in the caller's database transaction."""
    normalized_run_id = str(run_id).strip()
    if not normalized_run_id or len(normalized_run_id) > 36:
        raise ValueError("Invalid event-discovery run identifier")
    job_id = f"cignal-event-discovery-{normalized_run_id}"
    args = [normalized_run_id, dict(parameters or {})]
    existing = next(
        (item for item in db.new if isinstance(item, BackgroundJobOutbox) and item.job_id == job_id),
        None,
    )
    if existing is None:
        existing = db.query(BackgroundJobOutbox).filter_by(job_id=job_id).one_or_none()
    if existing is not None:
        if existing.function_path != "app.worker.discover_event_hypotheses" or existing.task_args != args:
            raise ValueError("A durable job identifier is already bound to different work")
        return existing
    row = BackgroundJobOutbox(
        id=str(uuid4()),
        job_id=job_id,
        function_path="app.worker.discover_event_hypotheses",
        task_args=args,
        queue_name="cignal",
        job_timeout_seconds=1800,
        result_ttl_seconds=86400,
        status="pending",
        attempts=0,
        available_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(row)
    return row


def enqueue_event_replay(db: Session, replay_id: str) -> BackgroundJobOutbox:
    """Persist a deterministic event-replay request in the caller transaction."""
    normalized_id = str(replay_id).strip()
    if not normalized_id or len(normalized_id) > 36:
        raise ValueError("Invalid event replay identifier")
    job_id = f"cignal-event-replay-{normalized_id}"
    args = [normalized_id]
    existing = next(
        (item for item in db.new if isinstance(item, BackgroundJobOutbox) and item.job_id == job_id),
        None,
    )
    if existing is None:
        existing = db.query(BackgroundJobOutbox).filter_by(job_id=job_id).one_or_none()
    if existing is not None:
        if existing.function_path != "app.worker.replay_event_history" or existing.task_args != args:
            raise ValueError("A durable replay job identifier is already bound to different work")
        return existing
    row = BackgroundJobOutbox(
        id=str(uuid4()), job_id=job_id,
        function_path="app.worker.replay_event_history", task_args=args,
        queue_name="cignal", job_timeout_seconds=1800, result_ttl_seconds=86400,
        status="pending", attempts=0, available_at=utcnow(),
        created_at=utcnow(), updated_at=utcnow(),
    )
    db.add(row)
    return row


def _claim_batch(session: Session, *, batch_size: int, lease_seconds: int, token: str) -> list[dict]:
    now = utcnow()
    eligible = or_(
        and_(BackgroundJobOutbox.status == "pending", BackgroundJobOutbox.available_at <= now),
        and_(
            BackgroundJobOutbox.status == "publishing",
            or_(
                BackgroundJobOutbox.lease_expires_at.is_(None),
                BackgroundJobOutbox.lease_expires_at <= now,
            ),
        ),
    )
    rows = (
        session.query(BackgroundJobOutbox)
        .filter(eligible)
        .order_by(BackgroundJobOutbox.available_at.asc(), BackgroundJobOutbox.created_at.asc())
        .limit(max(1, min(500, int(batch_size))))
        .with_for_update(skip_locked=True)
        .all()
    )
    claimed = []
    for row in rows:
        row.status = "publishing"
        row.attempts = int(row.attempts or 0) + 1
        row.lease_token = token
        row.lease_expires_at = now + timedelta(seconds=max(15, int(lease_seconds)))
        row.updated_at = now
        claimed.append({
            "id": row.id,
            "job_id": row.job_id,
            "function_path": row.function_path,
            "task_args": list(row.task_args or []),
            "queue_name": row.queue_name,
            "job_timeout_seconds": int(row.job_timeout_seconds),
            "result_ttl_seconds": int(row.result_ttl_seconds),
            "attempts": int(row.attempts),
        })
    session.commit()
    return claimed


def _mark_enqueued(session_factory: Callable, job_id: str, token: str) -> bool:
    with session_factory() as session:
        row = session.query(BackgroundJobOutbox).filter_by(job_id=job_id).one_or_none()
        if row is None or row.status != "publishing" or row.lease_token != token:
            session.rollback()
            return False
        row.status = "enqueued"
        row.enqueued_at = utcnow()
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error_code = None
        row.updated_at = utcnow()
        session.commit()
        return True


def _mark_failed(
    session_factory: Callable,
    job_id: str,
    token: str,
    error: Exception,
    *,
    attempts: int,
    max_attempts: int,
) -> str:
    with session_factory() as session:
        row = session.query(BackgroundJobOutbox).filter_by(job_id=job_id).one_or_none()
        if row is None or row.status != "publishing" or row.lease_token != token:
            session.rollback()
            return "lease_lost"
        now = utcnow()
        row.last_error_code = type(error).__name__[:80]
        row.lease_token = None
        row.lease_expires_at = None
        row.updated_at = now
        if attempts >= max_attempts:
            row.status = "dead"
            outcome = "dead"
        else:
            delay = min(3600, 5 * (2 ** min(max(0, attempts - 1), 9)))
            row.status = "pending"
            row.available_at = now + timedelta(seconds=delay)
            outcome = "retried"
        session.commit()
        return outcome


def dispatch_pending_jobs(
    queue,
    *,
    session_factory: Callable | None = None,
    batch_size: int = 100,
    lease_seconds: int = 120,
    max_attempts: int = 12,
) -> dict[str, int]:
    """Lease and enqueue due outbox jobs; duplicate RQ delivery is deterministic."""
    factory = session_factory or SessionLocal
    token = str(uuid4())
    with factory() as session:
        claimed = _claim_batch(
            session, batch_size=batch_size, lease_seconds=lease_seconds, token=token
        )

    result = {"claimed": len(claimed), "enqueued": 0, "retried": 0, "dead": 0, "lease_lost": 0}
    for item in claimed:
        try:
            if item["function_path"] not in _ALLOWED_FUNCTIONS:
                raise ValueError("Unsupported durable background function")
            existing = queue.fetch_job(item["job_id"])
            if existing is not None:
                state = existing.get_status(refresh=True)
                if state not in _ACTIVE_RQ_STATES:
                    existing.delete()
                    existing = None
            if existing is None:
                try:
                    queue.enqueue(
                        item["function_path"],
                        *item["task_args"],
                        job_id=item["job_id"],
                        job_timeout=item["job_timeout_seconds"],
                        result_ttl=item["result_ttl_seconds"],
                    )
                except Exception:
                    # A competing scheduler may have enqueued the same deterministic
                    # RQ ID after our fetch. Treat an active/finished winner as success.
                    raced = queue.fetch_job(item["job_id"])
                    if raced is None or raced.get_status(refresh=True) not in _ACTIVE_RQ_STATES:
                        raise
            marked = _mark_enqueued(factory, item["job_id"], token)
            if marked:
                result["enqueued"] += 1
            else:
                result["lease_lost"] += 1
        except Exception as exc:
            outcome = _mark_failed(
                factory,
                item["job_id"],
                token,
                exc,
                attempts=item["attempts"],
                max_attempts=max(1, int(max_attempts)),
            )
            result[outcome] = result.get(outcome, 0) + 1
            logger.warning(
                "Durable job dispatch failed job_id=%s outcome=%s error=%s",
                item["job_id"], outcome, type(exc).__name__,
            )
    return result


def summarize_outbox(session: Session, *, recent_limit: int = 25) -> dict:
    """Return bounded operational metadata without exposing job arguments."""
    counts = {status: 0 for status in ("pending", "publishing", "enqueued", "dead")}
    for status, count in (
        session.query(BackgroundJobOutbox.status, func.count())
        .group_by(BackgroundJobOutbox.status)
        .all()
    ):
        counts[str(status)] = int(count)
    rows = (
        session.query(BackgroundJobOutbox)
        .order_by(BackgroundJobOutbox.created_at.desc())
        .limit(max(1, min(100, int(recent_limit))))
        .all()
    )
    return {
        "counts": counts,
        "recent": [
            {
                "job_id": row.job_id,
                "function_path": row.function_path,
                "status": row.status,
                "attempts": row.attempts,
                "available_at": row.available_at.isoformat() if row.available_at else None,
                "enqueued_at": row.enqueued_at.isoformat() if row.enqueued_at else None,
                "last_error_code": row.last_error_code,
            }
            for row in rows
        ],
    }
