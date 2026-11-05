"""Read models for persisted connector ingestion telemetry."""
from __future__ import annotations

from datetime import UTC, timedelta

from sqlalchemy.orm import Session

from app.db.models import IntegrationRun, utcnow


def _as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def integration_run_dict(run: IntegrationRun) -> dict:
    return {
        "id": run.id,
        "source_type": run.source_type,
        "source_name": run.source_name,
        "trigger": run.trigger,
        "status": run.status,
        "records_fetched": run.records_fetched,
        "fetch_attempts": int(run.fetch_attempts or 1),
        "records_inserted": run.records_inserted,
        "records_duplicate": run.records_duplicate,
        "records_rejected": run.records_rejected,
        "records_queued": run.records_queued,
        "queue_failures": run.queue_failures,
        "rejection_counts": run.rejection_counts or {},
        "failure_code": run.failure_code,
        "retryable": bool(run.retryable),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_ms": run.duration_ms,
    }


def connector_health(db: Session, connectors: list[dict]) -> list[dict]:
    """Combine configuration with latest persisted run outcomes; never imply reachability from config alone."""
    now = utcnow()
    cutoff = now - timedelta(hours=24)
    rows: list[dict] = []

    for connector in connectors:
        source_type = connector["source_type"]
        query = db.query(IntegrationRun).filter(IntegrationRun.source_type == source_type)
        latest = query.order_by(IntegrationRun.started_at.desc(), IntegrationRun.id.desc()).first()
        latest_success = (
            query.filter(IntegrationRun.status.in_(("succeeded", "partial", "empty")))
            .order_by(IntegrationRun.started_at.desc(), IntegrationRun.id.desc())
            .first()
        )
        recent = query.filter(IntegrationRun.started_at >= cutoff).all()
        if not connector.get("configured"):
            health_status = "needs_configuration"
        elif latest is None:
            health_status = "not_tested"
        elif latest.status == "running":
            started = _as_utc(latest.started_at)
            health_status = (
                "stale" if started and started < now - timedelta(hours=2) else "running"
            )
        elif latest.status == "failed":
            health_status = "error"
        elif latest.status == "partial":
            health_status = "degraded"
        elif _as_utc(latest.completed_at) and _as_utc(latest.completed_at) < now - timedelta(days=2):
            health_status = "stale"
        else:
            health_status = "healthy"

        rows.append({
            **connector,
            "health_status": health_status,
            "latest_run": integration_run_dict(latest) if latest else None,
            "last_successful_at": latest_success.completed_at.isoformat()
            if latest_success and latest_success.completed_at else None,
            "runs_last_24h": len(recent),
            "inserted_last_24h": sum(item.records_inserted for item in recent),
            "failed_last_24h": sum(1 for item in recent if item.status == "failed"),
        })
    return rows
