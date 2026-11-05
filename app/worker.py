import hashlib
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from uuid import uuid4

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.models import (
    AcquisitionBackfillItem,
    AcquisitionBackfillRun,
    AcquisitionObservation,
    EventDiscoveryRun,
    EventReplayRun,
    Evidence,
    IntegrationRun,
    Notification,
    PairCandidateMiningRun,
    RefreshToken,
    Report,
)
from app.db.session import SessionLocal, engine, initialize_database
from app.intelligence.correlation import rank_duplicate_candidates
from app.intelligence.pipeline import analyze_multimodal_report
from app.services.events import enqueue_job, publish_event
from app.services.job_outbox import enqueue_report_analysis
from app.services.model_artifacts import archive_model_directory
from app.storage.factory import get_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cignal.worker")


def process_report(report_id: str, analysis_revision: int | None = None) -> dict:
    """Analyze one immutable report generation and discard stale computations.

    The generation is advanced whenever report text or evidence changes. A worker
    may finish after a newer generation was requested; in that case it must not
    overwrite the newer state and instead queues the current generation.
    """
    db = SessionLocal()
    latest_revision_to_queue = None
    result = None
    try:
        report = db.get(Report, report_id)
        if not report:
            return {"ok": False, "reason": "report_not_found"}

        current_revision = max(1, int(report.analysis_revision or 1))
        target_revision = (
            max(1, int(analysis_revision)) if analysis_revision is not None else current_revision
        )
        if target_revision != current_revision:
            latest_revision_to_queue = current_revision
            result = {
                "ok": True, "status": "superseded", "report_id": report_id,
                "requested_revision": target_revision, "current_revision": current_revision,
            }
        elif int(report.analyzed_revision or 0) >= target_revision:
            result = {
                "ok": True, "status": "already_current", "report_id": report_id,
                "analysis_revision": target_revision,
            }
        else:
            evidence_rows = (
                db.query(Evidence)
                .filter_by(report_id=report.id)
                .order_by(Evidence.created_at.asc())
                .all()
            )
            storage = get_storage()
            evidence_items = []
            analyzed_image_slots = 0
            image_limit = max(1, min(32, int(settings.ai_max_images_per_report)))
            for evidence in evidence_rows:
                if not evidence.mime_type.startswith("image/"):
                    evidence_items.append({
                        "id": evidence.id, "filename": evidence.original_filename,
                        "mime_type": evidence.mime_type,
                    })
                    continue
                analyzed_image_slots += 1
                if analyzed_image_slots > image_limit:
                    evidence_items.append({
                        "id": evidence.id, "filename": evidence.original_filename,
                        "mime_type": evidence.mime_type,
                    })
                    continue
                try:
                    content = storage.read(evidence.stored_key)
                except Exception as exc:
                    logger.warning("Unable to read image evidence %s: %s", evidence.id, type(exc).__name__)
                    content = b""
                evidence_items.append({
                    "id": evidence.id,
                    "filename": evidence.original_filename,
                    "mime_type": evidence.mime_type,
                    "content": content,
                    "expected_sha256": evidence.sha256,
                    "expected_size": evidence.size_bytes,
                    "stored_metadata": evidence.image_metadata or {},
                })

            analysis = analyze_multimodal_report(
                report.title, report.description, report.language, evidence_items,
                submitted_latitude=report.latitude, submitted_longitude=report.longitude,
                submitted_location_text=report.location_text,
            )
            text_analysis = analysis["text"]
            effective_disaster_type = (
                text_analysis["category"]
                if report.disaster_type in ("unknown", "other") else report.disaster_type
            )
            effective_urgency = (
                text_analysis["urgency"] if report.urgency == "unknown" else report.urgency
            )
            correlation_report = {
                "id": report.id,
                "title": report.title,
                "description": report.description,
                "disaster_type": effective_disaster_type,
                "latitude": report.latitude,
                "longitude": report.longitude,
                "created_at": report.created_at,
                "source_observed_at": report.source_observed_at,
            }
            candidates = (
                db.query(Report)
                .filter(
                    Report.id != report.id,
                    Report.created_at >= datetime.now(UTC) - timedelta(days=365),
                )
                .order_by(Report.created_at.desc())
                .limit(500)
                .all()
            )
            compatible_candidates = [
                item for item in candidates
                if item.disaster_type in (effective_disaster_type, "unknown", "other")
            ]
            ranked = rank_duplicate_candidates(
                correlation_report, compatible_candidates, limit=10, minimum_score=0.45
            )
            analysis["duplicate_candidates"] = ranked

            # Lock and refresh the latest report row before applying the computed result.
            # The revision comparison is the guard against evidence/follow-up races.
            locked_report = (
                db.query(Report)
                .filter(Report.id == report_id)
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
            if locked_report is None:
                db.rollback()
                result = {"ok": False, "reason": "report_not_found"}
            elif int(locked_report.analysis_revision or 1) != target_revision:
                latest_revision_to_queue = max(1, int(locked_report.analysis_revision or 1))
                db.rollback()
                result = {
                    "ok": True, "status": "superseded", "report_id": report_id,
                    "requested_revision": target_revision,
                    "current_revision": latest_revision_to_queue,
                }
            elif int(locked_report.analyzed_revision or 0) >= target_revision:
                db.rollback()
                result = {
                    "ok": True, "status": "already_current", "report_id": report_id,
                    "analysis_revision": target_revision,
                }
            else:
                locked_report.ai_category = text_analysis["category"]
                locked_report.ai_urgency = text_analysis["urgency"]
                locked_report.ai_confidence = text_analysis["confidence"]
                locked_report.ai_summary = analysis["summary"]
                locked_report.ai_analysis = analysis
                if locked_report.disaster_type in ("unknown", "other"):
                    locked_report.disaster_type = text_analysis["category"]
                if locked_report.urgency == "unknown":
                    locked_report.urgency = effective_urgency
                if locked_report.latitude is None and analysis["geospatial"]["latitude"] is not None:
                    locked_report.latitude = analysis["geospatial"]["latitude"]
                    locked_report.longitude = analysis["geospatial"]["longitude"]
                    locked_report.location_text = (
                        analysis["geospatial"]["location_text"]
                        or "Image EXIF location; requires human confirmation"
                    )[:2000]
                if locked_report.status == "submitted":
                    locked_report.status = "analyzed"
                if (
                    ranked and ranked[0]["automatic_duplicate_pointer_eligible"]
                    and not locked_report.duplicate_of
                ):
                    locked_report.duplicate_of = ranked[0]["candidate_id"]
                locked_report.analyzed_revision = target_revision

                vision_by_id = {
                    item.get("evidence_id"): item
                    for item in analysis["vision"]["images"]
                    if item.get("evidence_id")
                }
                for evidence in evidence_rows:
                    vision_result = vision_by_id.get(evidence.id)
                    if vision_result is not None:
                        evidence.image_metadata = {
                            **(evidence.image_metadata or {}),
                            "cignal_vision": vision_result,
                        }

                db.commit()
                publish_event("report.analyzed", {
                    "report_id": locked_report.id,
                    "status": locked_report.status,
                    "duplicate_of": locked_report.duplicate_of,
                    "analysis_revision": target_revision,
                    "ai_pipeline_version": analysis["pipeline_version"],
                })
                result = {
                    "ok": True,
                    "status": "analyzed",
                    "report_id": locked_report.id,
                    "analysis_revision": target_revision,
                    "duplicate_of": locked_report.duplicate_of,
                    "duplicate_candidate_count": len(ranked),
                    "analysis": analysis,
                }
    except Exception:
        db.rollback()
        logger.exception("Report analysis failed for %s", report_id)
        raise
    finally:
        db.close()

    if latest_revision_to_queue is not None:
        outbox_db = SessionLocal()
        try:
            enqueue_report_analysis(outbox_db, report_id, latest_revision_to_queue)
            outbox_db.commit()
            outboxed = True
        except Exception:
            outbox_db.rollback()
            logger.exception(
                "Could not persist superseding analysis request for report %s revision %s",
                report_id, latest_revision_to_queue,
            )
            outboxed = False
        finally:
            outbox_db.close()
        if result is not None:
            result["superseding_job_queued"] = outboxed
            result["superseding_job_outboxed"] = outboxed
    return result or {"ok": False, "reason": "analysis_not_completed"}


class SignalRejected(ValueError):
    """A source record failed validation and can be skipped without failing its feed."""


def _normalize_ingested_signal(signal, connector) -> dict:
    """Validate a connector record and return bounded values safe for persistence."""
    from app.integrations.base import (
        normalize_location_text,
        normalize_source_record_id,
        parse_datetime_utc,
        sanitize_source_url,
    )

    expected_source = str(connector.source_type or "").strip().lower()
    signal_source = str(getattr(signal, "source_type", "") or "").strip().lower()
    if not expected_source or len(expected_source) > 24 or signal_source != expected_source:
        raise SignalRejected("source_type_mismatch")

    title = " ".join(str(getattr(signal, "title", "") or "").split())
    if not title:
        raise SignalRejected("missing_title")

    latitude = getattr(signal, "latitude", None)
    longitude = getattr(signal, "longitude", None)
    if (latitude is None) != (longitude is None):
        raise SignalRejected("unpaired_coordinates")
    try:
        latitude = float(latitude) if latitude is not None else None
        longitude = float(longitude) if longitude is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise SignalRejected("invalid_coordinates") from exc
    if latitude is not None and (
        not math.isfinite(latitude) or not math.isfinite(longitude)
        or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
    ):
        raise SignalRejected("invalid_coordinates")

    observed_at = parse_datetime_utc(getattr(signal, "observed_at", None))
    observed_key = observed_at.isoformat() if observed_at else ""
    provider_metadata = getattr(signal, "source_metadata", None)
    raw_source_id = getattr(signal, "source_record_id", None)
    canonical_provider_id = None
    if expected_source == "nasa_eonet" and isinstance(provider_metadata, dict):
        candidate_id = str(provider_metadata.get("eonet_event_id") or "").strip()
        raw_identity = str(raw_source_id or "").strip()
        # Live EONET IDs carry a :payload-revision suffix; historical IDs are
        # already stable. Only canonicalize that known versioned form, leaving
        # custom connectors and synthetic fixtures untouched.
        if candidate_id and (raw_identity == candidate_id or raw_identity.startswith(f"{candidate_id}:")):
            canonical_provider_id = candidate_id
    source_id = normalize_source_record_id(
        canonical_provider_id or raw_source_id,
        fallback_material=(
            f"{expected_source}\0{title}\0{latitude}\0{longitude}\0{observed_key}"
        ),
    )
    description = " ".join(str(getattr(signal, "description", "") or "").split())[:10000]
    disaster_type = str(getattr(signal, "disaster_type", "unknown") or "unknown").strip().lower()[:32] or "unknown"
    urgency = str(getattr(signal, "urgency", "unknown") or "unknown").strip().lower()[:16] or "unknown"
    provenance = str(getattr(signal, "provenance", "live") or "live").strip().lower()[:20] or "live"
    source_url = sanitize_source_url(getattr(signal, "source_url", None))
    from app.integrations.source_catalog import build_report_source_metadata
    provider_metadata = getattr(signal, "source_metadata", None)
    source_metadata = build_report_source_metadata(expected_source, provider_metadata)

    return {
        "source_type": expected_source,
        "source_name": str(connector.name or expected_source).strip()[:100],
        "source_record_id": source_id,
        "title": title[:180],
        "description": description,
        "disaster_type": disaster_type,
        "urgency": urgency,
        "latitude": latitude,
        "longitude": longitude,
        "location_text": normalize_location_text(getattr(signal, "location_text", None)),
        "source_url": source_url,
        "provenance": provenance,
        "source_observed_at": observed_at,
        "source_metadata": source_metadata,
    }


def ingest_all_sources(
    trigger: str = "scheduled",
    source_type: str | None = None,
    lease_token: str | None = None,
) -> dict:
    """Run isolated connector ingests and persist source-level outcome telemetry.

    ``source_type`` scopes this job to one provider for independent scheduling.
    The existing global/manual mode remains supported. Provider identifiers are
    unique at the database boundary, making reruns safe after worker interruption.
    """
    from app.integrations.registry import connectors
    from app.integrations.retry import fetch_with_retry, is_retryable_connector_error
    from app.services.acquisition_schedule import complete_source_run, mark_source_started

    trigger = trigger if trigger in {"scheduled", "manual", "system"} else "system"
    selected_source = str(source_type or "").strip().lower() or None
    db = SessionLocal()
    inserted_total = duplicate_total = rejected_total = queued_total = queue_failures_total = 0
    failures = []
    source_runs = []
    attempted_report_ids: set[str] = set()
    backlog_queued = backlog_queue_failures = 0
    try:
        registered_connectors = connectors()
        if selected_source:
            registered_connectors = [
                item for item in registered_connectors
                if str(item.source_type).strip().lower() == selected_source
            ]
            if not registered_connectors:
                return {"ok": False, "reason": "source_not_registered", "source_type": selected_source}

        for connector in registered_connectors:
            if not connector.configured():
                continue

            if not mark_source_started(db, str(connector.source_type), lease_token if selected_source else None):
                source_runs.append({
                    "source_type": str(connector.source_type),
                    "status": "skipped",
                    "reason": "stale_or_expired_lease",
                })
                continue

            started_at = datetime.now(UTC)
            run = IntegrationRun(
                id=str(uuid4()),
                source_type=str(connector.source_type)[:24],
                source_name=str(connector.name)[:100],
                trigger=trigger,
                status="running",
                fetch_attempts=0,
                started_at=started_at,
                rejection_counts={},
            )
            db.add(run)
            db.commit()
            analysis_candidate_ids = []
            rejection_counts: dict[str, int] = {}
            fetched = duplicates = rejected = queued = queue_failures = 0
            fetch_attempts = 0
            committed_inserted = pending_inserted = 0

            try:
                def log_retry(
                    attempt: int,
                    delay: float,
                    exc: Exception,
                    source_type: str = connector.source_type,
                ) -> None:
                    logger.warning(
                        "Retrying connector %s after attempt %s in %.2fs (%s)",
                        source_type, attempt, delay, type(exc).__name__,
                    )

                signals, fetch_attempts = fetch_with_retry(
                    connector.fetch,
                    max_attempts=settings.integration_retry_attempts,
                    base_delay_seconds=settings.integration_retry_base_delay_seconds,
                    max_delay_seconds=settings.integration_retry_max_delay_seconds,
                    retryable=is_retryable_connector_error,
                    on_retry=log_retry,
                )
                fetched = len(signals)

                def begin_record_transaction() -> None:
                    # pysqlite's legacy transaction mode does not emit BEGIN for a
                    # SAVEPOINT. Explicit BEGIN keeps nested insert savepoints inside
                    # the connector transaction so a later failure can roll back the
                    # current uncommitted batch (PostgreSQL starts transactions normally).
                    if db.bind.dialect.name == "sqlite":
                        db.connection().exec_driver_sql("BEGIN")

                begin_record_transaction()
                for index, signal in enumerate(signals, start=1):
                    try:
                        normalized = _normalize_ingested_signal(signal, connector)
                    except (SignalRejected, ValueError) as exc:
                        reason = str(exc) if isinstance(exc, SignalRejected) else "invalid_source_record_id"
                        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                        rejected += 1
                        continue

                    from app.services.acquisition_archive import capture_observation
                    observation = capture_observation(db, normalized)
                    existing = db.query(Report).filter_by(
                        source_type=normalized["source_type"],
                        source_record_id=normalized["source_record_id"],
                    ).first()
                    if existing:
                        observation.report_id = existing.id
                        duplicates += 1
                        if existing.status == "submitted" and not existing.ai_category:
                            analysis_candidate_ids.append(existing.id)
                        continue

                    report = Report(
                        id=str(uuid4()),
                        tracking_code=f"CIG-S-{token_hex(4).upper()}",
                        title=normalized["title"],
                        description=normalized["description"],
                        disaster_type=normalized["disaster_type"],
                        urgency=normalized["urgency"],
                        status="submitted",
                        latitude=normalized["latitude"],
                        longitude=normalized["longitude"],
                        location_text=normalized["location_text"],
                        language="en",
                        source_type=normalized["source_type"],
                        source_name=normalized["source_name"],
                        source_record_id=normalized["source_record_id"],
                        source_url=normalized["source_url"],
                        provenance=normalized["provenance"],
                        source_observed_at=normalized["source_observed_at"],
                        source_metadata=normalized["source_metadata"],
                    )
                    try:
                        with db.begin_nested():
                            db.add(report)
                            db.flush()
                        pending_inserted += 1
                        analysis_candidate_ids.append(report.id)
                    except IntegrityError:
                        # Another worker may have inserted the same provider record.
                        duplicates += 1

                    if index % 100 == 0:
                        db.commit()
                        committed_inserted += pending_inserted
                        pending_inserted = 0
                        begin_record_transaction()

                db.commit()
                committed_inserted += pending_inserted
                pending_inserted = 0

                # Queue newly inserted reports and retry older pending reports. The
                # enqueue helper uses deterministic RQ job IDs to avoid duplicate jobs.
                for report_id in dict.fromkeys(analysis_candidate_ids):
                    attempted_report_ids.add(report_id)
                    try:
                        report_revision = db.query(Report.analysis_revision).filter_by(id=report_id).scalar() or 1
                        job = enqueue_job("app.worker.process_report", report_id, report_revision)
                    except Exception as exc:
                        logger.error("Unable to enqueue analysis job for report %s (%s)", report_id, type(exc).__name__)
                        job = None
                    if job is None:
                        queue_failures += 1
                    else:
                        queued += 1

                status = "empty" if fetched == 0 else "partial" if rejected or queue_failures else "succeeded"
                run.status = status
                run.records_fetched = fetched
                run.records_inserted = committed_inserted
                run.fetch_attempts = fetch_attempts
                run.records_duplicate = duplicates
                run.records_rejected = rejected
                run.records_queued = queued
                run.queue_failures = queue_failures
                run.rejection_counts = rejection_counts
                run.completed_at = datetime.now(UTC)
                run.duration_ms = max(0, int((run.completed_at - started_at).total_seconds() * 1000))
                run.retryable = False
                db.commit()

            except Exception as exc:
                fetch_attempts = max(
                    fetch_attempts, int(getattr(exc, "cignal_fetch_attempts", 0) or 0)
                )
                db.rollback()
                logger.error("Connector failed: %s (%s)", connector.name, type(exc).__name__)
                run = db.get(IntegrationRun, run.id)
                if run is not None:
                    run.status = "failed"
                    run.records_fetched = fetched
                    run.records_inserted = committed_inserted
                    run.fetch_attempts = fetch_attempts
                    run.records_duplicate = duplicates
                    run.records_rejected = rejected
                    run.records_queued = queued
                    run.queue_failures = queue_failures
                    run.rejection_counts = rejection_counts
                    run.failure_code = type(exc).__name__[:80]
                    run.retryable = is_retryable_connector_error(exc)
                    run.completed_at = datetime.now(UTC)
                    run.duration_ms = max(0, int((run.completed_at - started_at).total_seconds() * 1000))
                    db.commit()
                failures.append({
                    "source": str(connector.source_type),
                    "error": type(exc).__name__,
                    "retryable": is_retryable_connector_error(exc),
                })

            run = db.get(IntegrationRun, run.id)
            if run is not None:
                complete_source_run(
                    db,
                    source_type=run.source_type,
                    status=run.status,
                    run_id=run.id,
                    records_fetched=run.records_fetched,
                    records_inserted=run.records_inserted,
                    records_duplicate=run.records_duplicate,
                    records_rejected=run.records_rejected,
                    failure_code=run.failure_code,
                    retryable=bool(run.retryable),
                    lease_token=lease_token if selected_source else None,
                )
                source_runs.append({
                    "id": run.id,
                    "source_type": run.source_type,
                    "status": run.status,
                    "records_fetched": run.records_fetched,
                    "fetch_attempts": run.fetch_attempts,
                    "records_inserted": run.records_inserted,
                    "records_duplicate": run.records_duplicate,
                    "records_rejected": run.records_rejected,
                    "records_queued": run.records_queued,
                    "queue_failures": run.queue_failures,
                    "failure_code": run.failure_code,
                })
                inserted_total += run.records_inserted
                duplicate_total += run.records_duplicate
                rejected_total += run.records_rejected
                queued_total += run.records_queued
                queue_failures_total += run.queue_failures
                publish_event("integration.run.completed", source_runs[-1])

        # Recover citizen/historical submissions whose initial queue operation failed,
        # and stale pending reports after worker restarts. RQ job IDs make this safe
        # when an equivalent analysis job is already queued or running.
        if selected_source is None:
            pending_rows = (
                db.query(Report.id, Report.analysis_revision)
                .filter(Report.analysis_revision > Report.analyzed_revision)
                .order_by(Report.created_at.asc())
                .limit(100)
                .all()
            )
            for report_id, report_revision in pending_rows:
                if report_id in attempted_report_ids:
                    continue
                try:
                    job = enqueue_job("app.worker.process_report", report_id, report_revision)
                except Exception as exc:
                    logger.error("Unable to enqueue pending analysis %s (%s)", report_id, type(exc).__name__)
                    job = None
                if job is None:
                    backlog_queue_failures += 1
                else:
                    backlog_queued += 1

        summary = {
            "ok": not failures and queue_failures_total == 0 and backlog_queue_failures == 0,
            "source_type": selected_source,
            "inserted": inserted_total,
            "duplicates": duplicate_total,
            "rejected": rejected_total,
            "queued": queued_total,
            "queue_failures": queue_failures_total + backlog_queue_failures,
            "backlog_queued": backlog_queued,
            "backlog_queue_failures": backlog_queue_failures,
            "failures": failures,
            "source_runs": source_runs,
        }
        if source_runs:
            publish_event("ingestion.completed", summary)
        return summary
    finally:
        db.close()


def discover_event_hypotheses(run_id: str, parameters: dict | None = None) -> dict:
    """Execute one analyst-requested event-hypothesis discovery run."""
    from app.intelligence.event_hypotheses import discover_event_hypotheses as discover

    db = SessionLocal()
    run = None
    started_at = datetime.now(UTC)
    try:
        run = db.get(EventDiscoveryRun, run_id)
        if run is None:
            return {"ok": False, "reason": "discovery_run_not_found"}
        if run.status in {"completed", "failed"}:
            return {"ok": True, "status": run.status, "run_id": run_id}

        effective_parameters = dict(parameters or run.parameters or {})
        run.status = "running"
        run.started_at = started_at
        run.failure_code = None
        db.commit()

        # Serialize discovery runs across multiple RQ workers. The transaction-level
        # advisory lock is released on commit/rollback and does not affect other jobs.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(714552348::bigint)"))
            run = (
                db.query(EventDiscoveryRun)
                .filter(EventDiscoveryRun.id == run_id)
                .populate_existing()
                .one_or_none()
            )
            if run is None:
                return {"ok": False, "reason": "discovery_run_not_found"}
            if run.status in {"completed", "failed"}:
                return {"ok": True, "status": run.status, "run_id": run_id}

        result = discover(
            db,
            requested_by=run.requested_by,
            discovery_run_id=run_id,
            window_hours=int(effective_parameters.get("window_hours", 72)),
            max_reports=int(effective_parameters.get("max_reports", 300)),
            minimum_pair_score=float(effective_parameters.get("minimum_pair_score", 0.62)),
            max_distance_km=float(effective_parameters.get("max_distance_km", 25.0)),
        )
        completed_at = datetime.now(UTC)
        run = db.get(EventDiscoveryRun, run_id)
        run.status = "completed"
        run.reports_scanned = result["reports_scanned"]
        run.clusters_found = result["clusters_found"]
        run.hypotheses_created = result["hypotheses_created"]
        run.hypotheses_updated = result["hypotheses_updated"]
        run.clusters_skipped = result["clusters_skipped"]
        run.result_summary = {
            "hypothesis_snapshots_written": result.get("hypothesis_snapshots_written", 0),
            "semantic": result.get("semantic", {"status": "unknown"}),
            "supervised_pair_model": result.get("supervised_pair_model", {"available": False}),
            "scoring_version": result.get("scoring_version"),
        }
        run.completed_at = completed_at
        run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
        db.commit()
        publish_event("event.hypotheses.discovered", {
            "run_id": run_id,
            "status": "completed",
            "hypotheses_created": run.hypotheses_created,
            "hypotheses_updated": run.hypotheses_updated,
        })
        return {"ok": True, "status": "completed", **result, "run_id": run_id}
    except Exception as exc:
        db.rollback()
        failed = db.get(EventDiscoveryRun, run_id)
        if failed is not None:
            completed_at = datetime.now(UTC)
            failed.status = "failed"
            failed.failure_code = type(exc).__name__[:80]
            failed.completed_at = completed_at
            failed.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
            db.commit()
        logger.exception("Event hypothesis discovery failed for run %s", run_id)
        raise
    finally:
        db.close()


def run_worker() -> None:
    """Run only the configured workload lanes; RQ handles warm SIGTERM shutdown."""
    initialize_database()
    connection = Redis.from_url(settings.redis_url)
    queue_names = [
        item.strip()
        for item in os.environ.get("CIGNAL_WORKER_QUEUES", "cignal-priority,cignal").split(",")
        if item.strip()
    ]
    if not queue_names:
        raise RuntimeError("CIGNAL_WORKER_QUEUES must contain at least one queue")
    queues = [Queue(name, connection=connection) for name in queue_names]
    logger.info(
        "Cignal worker starting lanes=%s hostname=%s pid=%s",
        queue_names, socket.gethostname(), os.getpid(),
    )
    # Cignal owns scheduling/retries in PostgreSQL. RQ's embedded scheduler is disabled.
    # RQ's default signal handlers perform a warm shutdown on SIGTERM; Compose gives
    # the worker enough grace to finish the longest supported task timeout.
    Worker(queues, connection=connection).work(with_scheduler=False)


def apply_retention_policy() -> dict:
    """Apply configurable retention: remove old evidence and personal linkage, not incident history."""
    if not settings.retention_enabled:
        return {"ok": True, "skipped": "retention_disabled"}
    cutoff = datetime.now(UTC) - timedelta(days=max(30, settings.retention_days))
    db = SessionLocal()
    evidence_deleted = identities_anonymized = notifications_deleted = tokens_deleted = 0
    try:
        old_reports = db.query(Report).filter(Report.created_at < cutoff).all()
        evidence_keys = []
        for report in old_reports:
            if report.submitted_by is not None:
                report.submitted_by = None
                identities_anonymized += 1
            evidence_rows = db.query(Evidence).filter_by(report_id=report.id).all()
            for evidence in evidence_rows:
                evidence_keys.append(evidence.stored_key)
                db.delete(evidence)
                evidence_deleted += 1
        notifications_deleted = db.query(Notification).filter(Notification.created_at < cutoff).delete(synchronize_session=False)
        tokens_deleted = db.query(RefreshToken).filter(RefreshToken.expires_at < datetime.now(UTC)).delete(synchronize_session=False)
        db.commit()
        storage = get_storage()
        for key in evidence_keys:
            try:
                storage.delete(key)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Orphaned evidence cleanup failed for key %s", key)
        return {"ok": True, "evidence_deleted": evidence_deleted, "identities_anonymized": identities_anonymized, "notifications_deleted": notifications_deleted, "expired_tokens_deleted": tokens_deleted}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_scheduled_backup() -> dict:
    """Create a PostgreSQL custom dump and evidence archive with checksums."""
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = settings.backup_dir / f"cignal-backup-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    database_dump = target / "database.dump"
    evidence_archive = target / "evidence.tar.gz"
    models_archive = target / "models.tar.gz"
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError("Scheduled backup currently requires PostgreSQL")
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    command = [
        "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
        "--host", url.host or "localhost", "--port", str(url.port or 5432),
        "--username", url.username or "postgres", "--dbname", url.database or "cignal",
        "--file", str(database_dump),
    ]
    try:
        subprocess.run(command, check=True, env=env, capture_output=True, text=True, timeout=1800)
        with tarfile.open(evidence_archive, "w:gz") as archive:
            if settings.evidence_dir.exists():
                for file in settings.evidence_dir.rglob("*"):
                    if file.is_file():
                        archive.add(file, arcname=file.relative_to(settings.evidence_dir))
        model_root = settings.ai_text_model_path.parent
        model_files = archive_model_directory(model_root, models_archive)
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "database": "PostgreSQL custom format",
            "evidence_files": sum(1 for _ in settings.evidence_dir.rglob("*") if _.is_file()) if settings.evidence_dir.exists() else 0,
            "model_files": len(model_files),
            "model_artifacts": model_files,
            "retention_days": settings.retention_days,
            "application_version": "0.1.0",
        }
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        checks = []
        for file in (database_dump, evidence_archive, models_archive, target / "manifest.json"):
            digest = _sha256_file(file)
            checks.append(f"{digest}  {file.name}")
        (target / "SHA256SUMS.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")
        backups = sorted(settings.backup_dir.glob("cignal-backup-*"), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in backups[max(1, settings.backup_retention_count):]:
            shutil.rmtree(stale, ignore_errors=True)
        logger.info("Backup completed: %s", target)
        return {"ok": True, "path": str(target), "manifest": manifest, "models_archive": str(models_archive)}
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        logger.exception("Scheduled backup failed")
        raise


def run_evidence_integrity_audit(run_id: str | None = None) -> dict:
    """Run a bounded, auditable verification of stored evidence bytes."""
    from uuid import uuid4

    from app.services.audit import record_audit
    from app.services.evidence_integrity import audit_evidence_integrity

    audit_run_id = run_id or str(uuid4())
    db = SessionLocal()
    try:
        summary = audit_evidence_integrity(db, run_id=audit_run_id)
        logger.info(
            "Evidence integrity audit %s completed: checked=%s passed=%s failed=%s",
            audit_run_id, summary["checked"], summary["passed"], summary["failed"],
        )
        return summary
    except Exception as exc:
        db.rollback()
        record_audit(
            db,
            "evidence.integrity_audit_failed",
            "evidence_integrity_audit",
            audit_run_id,
            details={"run_id": audit_run_id, "error_type": type(exc).__name__},
        )
        db.commit()
        logger.exception("Evidence integrity audit %s failed", audit_run_id)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_worker()



def _queue_pending_backfill_items(db, backfill_run_id: str, lease_token: str) -> tuple[int, int]:
    """Queue only this run's durable items and report the remaining failed count."""
    run = db.get(AcquisitionBackfillRun, backfill_run_id)
    if run is None or run.lease_token != lease_token:
        raise RuntimeError("stale_backfill_lease")
    run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    db.flush()

    items = (
        db.query(AcquisitionBackfillItem)
        .filter(
            AcquisitionBackfillItem.backfill_run_id == backfill_run_id,
            AcquisitionBackfillItem.analysis_status.in_(("pending", "failed")),
        )
        .order_by(AcquisitionBackfillItem.created_at.asc(), AcquisitionBackfillItem.id.asc())
        .with_for_update()
        .all()
    )
    queued = 0
    for item in items:
        report = db.get(Report, item.report_id) if item.report_id else None
        if report is None:
            item.analysis_status = "not_required"
            item.last_error_code = "linked_report_missing"
            continue
        target_revision = max(1, int(report.analysis_revision or 1))
        if int(report.analyzed_revision or 0) >= target_revision:
            item.analysis_status = "not_required"
            item.last_error_code = None
            continue
        item.queue_attempts = int(item.queue_attempts or 0) + 1
        try:
            job = enqueue_job("app.worker.process_report", report.id, target_revision)
        except Exception:
            job = None
        if job is None:
            item.analysis_status = "failed"
            item.last_error_code = "queue_unavailable"
        else:
            item.analysis_status = "queued"
            item.last_error_code = None
            queued += 1
        item.updated_at = datetime.now(UTC)

    db.flush()
    remaining_failures = (
        db.query(AcquisitionBackfillItem)
        .filter(
            AcquisitionBackfillItem.backfill_run_id == backfill_run_id,
            AcquisitionBackfillItem.analysis_status.in_(("pending", "failed")),
        )
        .count()
    )
    run = db.get(AcquisitionBackfillRun, backfill_run_id)
    run.records_queued += queued
    run.analysis_queue_failures = remaining_failures
    run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    run.updated_at = datetime.now(UTC)
    db.commit()
    return queued, remaining_failures


def run_acquisition_backfill(backfill_run_id: str) -> dict:
    """Resume one bounded historical backfill from its last committed page cursor."""
    from app.integrations.registry import connectors
    from app.services.acquisition_archive import capture_observation

    db = SessionLocal()
    try:
        run = db.query(AcquisitionBackfillRun).filter_by(id=backfill_run_id).with_for_update().one_or_none()
        if run is None:
            return {"ok": False, "reason": "backfill_run_not_found"}
        if run.status == "completed":
            return {"ok": True, "status": "completed", "run_id": run.id}
        now = datetime.now(UTC)
        current_lease = run.lease_expires_at
        if current_lease is not None and current_lease.tzinfo is None:
            current_lease = current_lease.replace(tzinfo=UTC)
        if run.lease_token and current_lease and current_lease > now:
            return {"ok": False, "reason": "backfill_already_running", "run_id": run.id}
        lease_token = str(uuid4())
        run.lease_token = lease_token
        run.lease_expires_at = now + timedelta(minutes=10)
        run.status = "running"
        run.started_at = now
        run.last_error_code = None
        db.commit()

        connector = next((item for item in connectors() if item.source_type == run.source_type), None)
        if connector is None or not connector.configured():
            raise RuntimeError("historical_source_not_configured")
        fetch_window = getattr(connector, "fetch_window", None)
        if not callable(fetch_window):
            raise RuntimeError("historical_backfill_not_supported_for_source")
        page_size = 500 if run.source_type == "usgs" else 2000

        def as_utc(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        while as_utc(run.cursor_at) <= as_utc(run.end_at) and run.records_fetched < run.max_records:
            run = db.get(AcquisitionBackfillRun, backfill_run_id)
            if run is None or run.lease_token != lease_token:
                return {"ok": False, "reason": "stale_backfill_lease", "run_id": backfill_run_id}
            cursor = as_utc(run.cursor_at)
            run_end = min(
                cursor + timedelta(days=max(1, min(14, int(run.window_days)))) - timedelta(microseconds=1),
                as_utc(run.end_at),
            )
            remaining = max(1, int(run.max_records) - int(run.records_fetched))
            limit = min(page_size, remaining)
            provider_page_complete = True
            if run.source_type == "usgs":
                signals = fetch_window(cursor, run_end, offset=max(1, int(run.page_offset)), limit=limit)
            else:
                # EONET is a bounded result list rather than an offset API. A stable
                # local offset makes record caps resumable without skipping results.
                all_signals = fetch_window(cursor, run_end)
                if len(all_signals) >= page_size:
                    raise RuntimeError("historical_window_may_be_truncated_reduce_window_days")
                all_signals = sorted(all_signals, key=lambda item: str(item.source_record_id))
                start_index = max(0, int(run.page_offset) - 1)
                signals = all_signals[start_index:start_index + remaining]
                provider_page_complete = start_index + len(signals) >= len(all_signals)

            run.records_fetched += len(signals)
            inserted_page = duplicate_page = rejected_page = captured_page = 0
            for signal in signals:
                try:
                    normalized = _normalize_ingested_signal(signal, connector)
                except (SignalRejected, ValueError):
                    rejected_page += 1
                    continue

                observation = capture_observation(db, normalized)
                captured_page += 1
                existing = db.query(Report).filter_by(
                    source_type=normalized["source_type"],
                    source_record_id=normalized["source_record_id"],
                ).one_or_none()
                if existing is not None:
                    report = existing
                    observation.report_id = existing.id
                    duplicate_page += 1
                else:
                    report = Report(
                        id=str(uuid4()), tracking_code=f"CIG-H-{token_hex(4).upper()}",
                        title=normalized["title"], description=normalized["description"],
                        disaster_type=normalized["disaster_type"], urgency=normalized["urgency"],
                        status="submitted", latitude=normalized["latitude"], longitude=normalized["longitude"],
                        location_text=normalized["location_text"], language="en",
                        source_type=normalized["source_type"], source_name=normalized["source_name"],
                        source_record_id=normalized["source_record_id"], source_url=normalized["source_url"],
                        provenance="historical", source_observed_at=normalized["source_observed_at"],
                        source_metadata={**normalized["source_metadata"], "acquisition_mode": "historical_backfill"},
                    )
                    try:
                        with db.begin_nested():
                            db.add(report)
                            db.flush()
                        observation.report_id = report.id
                        inserted_page += 1
                    except IntegrityError as err:
                        report = db.query(Report).filter_by(
                            source_type=normalized["source_type"],
                            source_record_id=normalized["source_record_id"],
                        ).one_or_none()
                        if report is None:
                            raise RuntimeError(
                                "report_conflict_without_existing_source_record"
                            ) from err
                        observation.report_id = report.id
                        duplicate_page += 1

                item = db.query(AcquisitionBackfillItem).filter_by(
                    backfill_run_id=run.id, observation_id=observation.id,
                ).one_or_none()
                if item is None:
                    needs_analysis = int(report.analyzed_revision or 0) < max(1, int(report.analysis_revision or 1))
                    item = AcquisitionBackfillItem(
                        id=str(uuid4()), backfill_run_id=run.id,
                        observation_id=observation.id, report_id=report.id,
                        analysis_status="pending" if needs_analysis else "not_required",
                        queue_attempts=0, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                    )
                    db.add(item)
                else:
                    item.report_id = report.id
                    if item.analysis_status in {"pending", "failed"} and int(report.analyzed_revision or 0) >= max(1, int(report.analysis_revision or 1)):
                        item.analysis_status = "not_required"
                        item.last_error_code = None

            # Commit immutable observations, reports, run-item links and cursor together.
            run.records_inserted += inserted_page
            run.records_duplicate += duplicate_page
            run.records_rejected += rejected_page
            run.observations_captured += captured_page
            if run.source_type == "usgs" and len(signals) == limit:
                run.page_offset += len(signals)
            elif run.source_type != "usgs" and not provider_page_complete:
                run.page_offset += len(signals)
            else:
                run.cursor_at = run_end + timedelta(microseconds=1)
                run.page_offset = 1
                run.windows_completed += 1
            run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            run.updated_at = datetime.now(UTC)
            db.commit()

            _queue_pending_backfill_items(db, backfill_run_id, lease_token)

            run = db.get(AcquisitionBackfillRun, backfill_run_id)
            if run.source_type == "usgs" and len(signals) == limit and run.records_fetched < run.max_records:
                continue
            if run.source_type != "usgs" and not provider_page_complete and run.records_fetched < run.max_records:
                continue
            if not signals and as_utc(run.cursor_at) <= cursor:
                run.cursor_at = run_end + timedelta(microseconds=1)
                run.page_offset = 1
                run.windows_completed += 1
                db.commit()
            if run.records_fetched >= run.max_records:
                break

        run = db.get(AcquisitionBackfillRun, backfill_run_id)
        _, remaining_queue_failures = _queue_pending_backfill_items(db, backfill_run_id, lease_token)
        run = db.get(AcquisitionBackfillRun, backfill_run_id)
        if as_utc(run.cursor_at) > as_utc(run.end_at) and remaining_queue_failures == 0:
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
        elif run.records_fetched >= run.max_records:
            run.status = "limit_reached"
        else:
            run.status = "partial"
        run.lease_token = None
        run.lease_expires_at = None
        run.updated_at = datetime.now(UTC)
        db.commit()
        summary = {
            "ok": run.status == "completed",
            "run_id": run.id,
            "status": run.status,
            "source_type": run.source_type,
            "records_fetched": run.records_fetched,
            "records_inserted": run.records_inserted,
            "records_duplicate": run.records_duplicate,
            "records_rejected": run.records_rejected,
            "observations_captured": run.observations_captured,
            "records_queued": run.records_queued,
            "analysis_queue_failures": run.analysis_queue_failures,
            "windows_completed": run.windows_completed,
            "cursor_at": run.cursor_at.isoformat(),
        }
        publish_event("acquisition.backfill.completed", summary)
        return summary
    except Exception as exc:
        db.rollback()
        failed = db.get(AcquisitionBackfillRun, backfill_run_id)
        if failed is not None:
            failed.status = "failed"
            failed.last_error_code = type(exc).__name__[:80]
            failed.lease_token = None
            failed.lease_expires_at = None
            failed.completed_at = datetime.now(UTC)
            db.commit()
        logger.exception("Historical acquisition backfill failed: %s", type(exc).__name__)
        return {"ok": False, "run_id": backfill_run_id, "status": "failed", "error": type(exc).__name__}
    finally:
        db.close()


def mine_pair_candidates(run_id: str) -> dict:
    """Lease-protected RQ entry point for bounded, restart-safe candidate mining."""
    from app.services.active_learning import mine_event_pair_candidates

    db = SessionLocal()
    lease_token = str(uuid4())
    try:
        run = db.query(PairCandidateMiningRun).filter_by(id=run_id).with_for_update().one_or_none()
        if run is None:
            return {"ok": False, "reason": "mining_run_not_found", "run_id": run_id}
        if run.status == "completed":
            return {"ok": True, "status": "completed", "run_id": run_id}
        now = datetime.now(UTC)
        expiry = run.lease_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if run.lease_token and expiry and expiry > now:
            return {"ok": False, "reason": "mining_already_running", "run_id": run_id}
        run.lease_token = lease_token
        run.lease_expires_at = now + timedelta(minutes=35)
        run.status = "running"
        run.attempts = int(run.attempts or 0) + 1
        run.started_at = now
        run.updated_at = now
        run.failure_code = None
        db.commit()

        result = mine_event_pair_candidates(db, run_id=run_id)
        run = db.get(PairCandidateMiningRun, run_id)
        if run is None or run.lease_token != lease_token:
            return {"ok": False, "reason": "stale_mining_lease", "run_id": run_id}
        run.lease_token = None
        run.lease_expires_at = None
        run.updated_at = datetime.now(UTC)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        failed = db.get(PairCandidateMiningRun, run_id)
        if failed is not None and failed.lease_token == lease_token:
            failed.lease_token = None
            failed.lease_expires_at = None
            failed.status = "failed"
            failed.failure_code = type(exc).__name__[:80]
            failed.completed_at = datetime.now(UTC)
            failed.updated_at = datetime.now(UTC)
            db.commit()
        logger.exception("Pair candidate mining failed: %s", type(exc).__name__)
        raise
    finally:
        db.close()


def replay_event_history(replay_id: str) -> dict:
    """Execute a frozen-input event replay; canonical incidents are never mutated."""
    from app.services.event_replay import execute_event_replay

    db = SessionLocal()
    started_at = datetime.now(UTC)
    try:
        run = db.get(EventReplayRun, replay_id)
        if run is None:
            return {"ok": False, "reason": "replay_not_found"}
        if run.status == "completed":
            return {"ok": True, "status": "completed", "run_id": replay_id, "idempotent_replay": True}
        run.status = "running"
        run.started_at = started_at
        run.failure_code = None
        db.commit()
        result = execute_event_replay(db, replay_id)
        run = db.get(EventReplayRun, replay_id)
        if run is None:
            return {"ok": False, "reason": "replay_not_found"}
        completed_at = datetime.now(UTC)
        run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
        db.commit()
        try:
            publish_event("event.replay.completed", {"run_id": replay_id, "summary": result})
        except Exception as exc:
            # Notification fan-out is not part of the replay result transaction.
            logger.warning(
                "Event replay notification failed run=%s error=%s",
                replay_id, type(exc).__name__,
            )
        return result
    except Exception as exc:
        db.rollback()
        run = db.get(EventReplayRun, replay_id)
        if run is not None:
            run.status = "failed"
            run.failure_code = type(exc).__name__[:80]
            run.completed_at = datetime.now(UTC)
            run.duration_ms = max(0, int((run.completed_at - started_at).total_seconds() * 1000))
            db.commit()
        logger.exception("Event replay failed run=%s error=%s", replay_id, type(exc).__name__)
        return {"ok": False, "status": "failed", "run_id": replay_id, "failure_code": type(exc).__name__}
    finally:
        db.close()


def replay_acquisition_observation(observation_id: str) -> dict:
    """Requeue analysis for the report linked to an immutable observation snapshot."""
    db = SessionLocal()
    try:
        observation = db.get(AcquisitionObservation, observation_id)
        if observation is None:
            return {"ok": False, "reason": "observation_not_found"}
        observation.replay_attempts = int(observation.replay_attempts or 0) + 1
        observation.last_replayed_at = datetime.now(UTC)
        report = db.get(Report, observation.report_id) if observation.report_id else None
        if report is None:
            observation.last_error_code = "linked_report_missing"
            db.commit()
            return {"ok": False, "reason": "linked_report_missing"}
        revision = max(1, int(report.analysis_revision or 1))
        job = enqueue_job("app.worker.process_report", report.id, revision)
        observation.last_error_code = None if job else "queue_unavailable"
        db.commit()
        return {"ok": job is not None, "report_id": report.id, "job_id": job.id if job else None}
    finally:
        db.close()



@contextmanager
def _outbox_execution_lock(outbox_id: str):
    """Fence duplicate executions across processes using a PostgreSQL session lock."""
    if engine.dialect.name != "postgresql":
        yield True
        return
    lock_key = int.from_bytes(
        hashlib.sha256(str(outbox_id).encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            ).scalar()
        )
        connection.commit()
        yield acquired
    finally:
        if acquired:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                connection.commit()
            except Exception:
                logger.warning("Unable to release job advisory lock for outbox=%s", outbox_id)
        connection.close()


def execute_outbox_job(outbox_id: str, rq_job_id: str) -> dict:
    """Execute one outbox request with a database lease and cross-process idempotency fence."""
    from app.services.job_execution import (
        claim_execution,
        complete_execution,
        fail_execution,
    )

    # RQ executes jobs in forked workhorse processes. Dispose the inherited
    # SQLAlchemy pool without closing parent-process connections so this child
    # creates its own DBAPI connections before taking a lease/advisory lock.
    engine.dispose(close=False)
    worker_name = f"{socket.gethostname()}:{os.getpid()}"
    with _outbox_execution_lock(outbox_id) as acquired:
        if not acquired:
            # Mark this RQ delivery failed rather than finished. The scheduler
            # can then reconcile it and retry after the active process releases
            # the advisory lock; returning success would falsely look completed.
            logger.warning("Duplicate RQ delivery deferred; execution fence is held outbox=%s rq_job=%s", outbox_id, rq_job_id)
            raise RuntimeError("outbox_execution_fence_busy")

        execution = claim_execution(
            outbox_id,
            rq_job_id=rq_job_id,
            worker_name=worker_name,
            lease_seconds=(
                max(30, int(settings.background_job_lease_grace_seconds))
                + max(30, int(_outbox_timeout_seconds(outbox_id)))
            ),
            max_attempts=max(1, int(settings.background_job_max_execution_attempts)),
        )
        state = execution.get("status")
        if state != "claimed":
            return {"ok": state in {"already_completed", "already_running"}, "status": state, "outbox_id": outbox_id}

        function_path = execution["function_path"]
        args = execution["task_args"]
        try:
            if function_path == "app.worker.process_report":
                result = process_report(*args)
            elif function_path == "app.worker.discover_event_hypotheses":
                result = discover_event_hypotheses(*args)
            elif function_path == "app.worker.replay_event_history":
                result = replay_event_history(*args)
            else:
                raise ValueError("Unsupported durable background function")

            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError(str(result.get("failure_code") or "target_function_failed")[:80])

            serialized = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
            result_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            completed = complete_execution(
                outbox_id,
                execution["attempt_id"],
                execution["lease_token"],
                result_sha256=result_sha256,
            )
            if not completed:
                logger.error("Outbox execution lost its fencing lease after target completion: %s", outbox_id)
                return {"ok": False, "status": "stale_execution_lease", "outbox_id": outbox_id}
            return {
                "ok": True,
                "status": "completed",
                "outbox_id": outbox_id,
                "job_id": execution["job_id"],
                "result_sha256": result_sha256,
            }
        except Exception as exc:
            outcome = fail_execution(
                outbox_id,
                execution["attempt_id"],
                execution["lease_token"],
                exc,
                max_attempts=max(1, int(settings.background_job_max_execution_attempts)),
            )
            logger.exception(
                "Durable job execution failed outbox=%s attempt=%s outcome=%s",
                outbox_id, execution["attempt_number"], outcome,
            )
            raise


def _outbox_timeout_seconds(outbox_id: str) -> int:
    """Read the bounded timeout for this logical job without exposing its arguments."""
    db = SessionLocal()
    try:
        from app.db.models import BackgroundJobOutbox

        row = db.get(BackgroundJobOutbox, outbox_id)
        return max(30, int(row.job_timeout_seconds if row else 300))
    finally:
        db.close()
