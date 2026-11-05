"""Durable per-source acquisition scheduling and recovery state.

The database is the authority for due times and leases. Redis/RQ is only the
transport for work; expired leases make crashed or lost jobs eligible again.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AcquisitionSourceState


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def configured_source_intervals() -> dict[str, int]:
    """Parse SOURCE=SECONDS overrides; invalid values are ignored safely."""
    minimum = 60
    maximum = 86400
    result: dict[str, int] = {}
    raw = str(settings.ingest_source_interval_overrides or "")
    for item in raw.split(";"):
        if "=" not in item:
            continue
        name, raw_seconds = item.split("=", 1)
        source_type = name.strip().lower()
        try:
            seconds = int(raw_seconds.strip())
        except (TypeError, ValueError):
            continue
        if source_type and len(source_type) <= 24 and minimum <= seconds <= maximum:
            result[source_type] = seconds
    return result


def source_poll_interval_seconds(source_type: str) -> int:
    overrides = configured_source_intervals()
    if source_type in overrides:
        return overrides[source_type]
    return max(300, min(86400, int(settings.ingest_interval_minutes) * 60))


def ensure_source_state(db: Session, source_type: str, *, now: datetime | None = None) -> AcquisitionSourceState:
    """Create schedule state lazily; first automatic poll waits one full interval."""
    current = as_utc(now or utcnow())
    state = db.get(AcquisitionSourceState, source_type)
    interval = source_poll_interval_seconds(source_type)
    if state is None:
        state = AcquisitionSourceState(
            source_type=source_type,
            poll_interval_seconds=interval,
            next_run_at=current + timedelta(seconds=interval),
            last_status="not_scheduled",
            consecutive_failures=0,
        )
        db.add(state)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            state = db.get(AcquisitionSourceState, source_type)
            if state is None:
                raise
    elif state.poll_interval_seconds != interval:
        state.poll_interval_seconds = interval
        db.commit()
    return state


def lease_is_current(
    db: Session,
    source_type: str,
    lease_token: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not lease_token:
        return True
    state = db.get(AcquisitionSourceState, source_type)
    current = as_utc(now or utcnow())
    return bool(
        state
        and state.lease_token == lease_token
        and as_utc(state.lease_expires_at)
        and as_utc(state.lease_expires_at) > current
    )


def mark_source_started(
    db: Session,
    source_type: str,
    lease_token: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    current = as_utc(now or utcnow())
    state = ensure_source_state(db, source_type, now=current)
    if lease_token and not lease_is_current(db, source_type, lease_token, now=current):
        return False
    active_lease = bool(
        state.lease_token
        and as_utc(state.lease_expires_at)
        and as_utc(state.lease_expires_at) > current
    )
    # A global/manual sweep may overlap a scheduled source job. It can still ingest
    # safely (the report unique constraint arbitrates duplicates), but it must not
    # overwrite the scheduler's active lease or running status.
    if not lease_token and active_lease:
        return True
    state.last_started_at = current
    state.last_status = "running"
    db.commit()
    return True


def claim_due_sources(
    db: Session,
    connectors: list,
    *,
    now: datetime | None = None,
    lease_seconds: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Claim due configured sources using row locks and expiring worker leases."""
    current = as_utc(now or utcnow())
    configured_lease = int(lease_seconds or settings.ingest_job_lease_seconds)
    lease_duration = max(
        300,
        configured_lease,
        int(settings.ingest_job_timeout_seconds) + 60,
    )
    due: list[dict] = []
    source_types = sorted({
        str(connector.source_type).strip().lower()
        for connector in connectors
        if connector.configured()
    })

    for source_type in source_types:
        if len(due) >= max(1, limit):
            break
        ensure_source_state(db, source_type, now=current)
        statement = (
            select(AcquisitionSourceState)
            .where(AcquisitionSourceState.source_type == source_type)
            .with_for_update(skip_locked=True)
        )
        state = db.execute(statement).scalar_one_or_none()
        if state is None:
            db.rollback()
            continue
        lease_expires = as_utc(state.lease_expires_at)
        next_run = as_utc(state.next_run_at)
        if (next_run and next_run > current) or (lease_expires and lease_expires > current):
            db.rollback()
            continue
        token = str(uuid4())
        state.lease_token = token
        state.lease_expires_at = current + timedelta(seconds=lease_duration)
        state.last_status = "queued"
        db.commit()
        due.append({
            "source_type": source_type,
            "lease_token": token,
            "lease_expires_at": (current + timedelta(seconds=lease_duration)).isoformat(),
            "job_timeout_seconds": int(settings.ingest_job_timeout_seconds),
        })
    return due


def release_source_claim(
    db: Session,
    source_type: str,
    lease_token: str,
    *,
    delay_seconds: int = 60,
    now: datetime | None = None,
) -> bool:
    state = db.get(AcquisitionSourceState, source_type)
    if state is None or state.lease_token != lease_token:
        return False
    current = as_utc(now or utcnow())
    state.lease_token = None
    state.lease_expires_at = None
    state.next_run_at = current + timedelta(seconds=max(15, int(delay_seconds)))
    state.last_status = "queue_failed"
    db.commit()
    return True


def complete_source_run(
    db: Session,
    *,
    source_type: str,
    status: str,
    run_id: str | None,
    records_fetched: int = 0,
    records_inserted: int = 0,
    records_duplicate: int = 0,
    records_rejected: int = 0,
    failure_code: str | None = None,
    retryable: bool = False,
    lease_token: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Persist source outcome and select its next due time with capped backoff."""
    current = as_utc(now or utcnow())
    state = ensure_source_state(db, source_type, now=current)
    if lease_token and state.lease_token != lease_token:
        return False
    active_lease = bool(
        state.lease_token
        and as_utc(state.lease_expires_at)
        and as_utc(state.lease_expires_at) > current
    )
    if not lease_token and active_lease:
        return False

    normalized_status = status if status in {"succeeded", "partial", "failed", "empty"} else "failed"
    state.last_completed_at = current
    state.last_run_id = run_id
    state.last_status = normalized_status
    state.last_records_fetched = max(0, int(records_fetched))
    state.last_records_inserted = max(0, int(records_inserted))
    state.last_records_duplicate = max(0, int(records_duplicate))
    state.last_records_rejected = max(0, int(records_rejected))
    state.last_error_code = (failure_code or "")[:80] or None

    if normalized_status == "failed":
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        if retryable:
            base = max(15, int(settings.ingest_retry_backoff_base_seconds))
            cap = max(base, int(settings.ingest_retry_backoff_max_seconds))
            delay = min(cap, base * (2 ** min(20, state.consecutive_failures - 1)))
        else:
            delay = int(state.poll_interval_seconds)
    else:
        state.consecutive_failures = 0
        delay = int(state.poll_interval_seconds)

    state.next_run_at = current + timedelta(seconds=delay)
    if not lease_token or state.lease_token == lease_token:
        state.lease_token = None
        state.lease_expires_at = None
    db.commit()
    return True


def source_schedule_dict(state: AcquisitionSourceState, *, now: datetime | None = None) -> dict:
    current = as_utc(now or utcnow())
    next_run = as_utc(state.next_run_at)
    lease_expiry = as_utc(state.lease_expires_at)
    return {
        "source_type": state.source_type,
        "poll_interval_seconds": state.poll_interval_seconds,
        "next_run_at": next_run.isoformat() if next_run else None,
        "seconds_until_next_run": max(0, int((next_run - current).total_seconds())) if next_run else None,
        "lease_expires_at": lease_expiry.isoformat() if lease_expiry else None,
        "lease_active": bool(lease_expiry and lease_expiry > current),
        "last_started_at": state.last_started_at.isoformat() if state.last_started_at else None,
        "last_completed_at": state.last_completed_at.isoformat() if state.last_completed_at else None,
        "last_run_id": state.last_run_id,
        "last_status": state.last_status,
        "consecutive_failures": state.consecutive_failures,
        "last_error_code": state.last_error_code,
        "last_records_fetched": state.last_records_fetched,
        "last_records_inserted": state.last_records_inserted,
        "last_records_duplicate": state.last_records_duplicate,
        "last_records_rejected": state.last_records_rejected,
    }


def source_schedule_status(db: Session, connectors: list) -> list[dict]:
    result = []
    for connector in connectors:
        if not connector.configured():
            continue
        state = db.get(AcquisitionSourceState, connector.source_type)
        if state is None:
            result.append({
                "source_type": connector.source_type,
                "poll_interval_seconds": source_poll_interval_seconds(connector.source_type),
                "next_run_at": None,
                "seconds_until_next_run": None,
                "lease_active": False,
                "last_status": "not_initialized",
            })
        else:
            result.append(source_schedule_dict(state))
    return result
