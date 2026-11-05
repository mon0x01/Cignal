"""Deterministic, non-mutating replay of event correlation over frozen report inputs.

Replay outputs are candidate analysis artifacts. This module never creates or edits
EventHypothesis, ReportIncident, Incident, warnings, or response actions.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    EventHypothesis,
    EventHypothesisReport,
    EventReplayBaseline,
    EventReplayInput,
    EventReplayResult,
    EventReplayRun,
    Report,
    utcnow,
)
from app.intelligence.event_hypotheses import SCORING_VERSION, build_candidate_clusters
from app.services.job_outbox import enqueue_event_replay

ENGINE_VERSION = "event-evolution-replay-v2-indexed"
MAX_WINDOW_DAYS = 366
MAX_INPUTS = 500


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _report_payload(report: Report) -> dict:
    """Minimal immutable input contract consumed by the correlation scorer."""
    observed = report.source_observed_at or report.created_at
    return {
        "id": str(report.id),
        "title": str(report.title or ""),
        "description": str(report.description or ""),
        "disaster_type": str(report.disaster_type or "unknown").lower(),
        "latitude": float(report.latitude),
        "longitude": float(report.longitude),
        "location_text": report.location_text,
        "language": str(report.language or "en"),
        "source_type": str(report.source_type or "unknown"),
        "source_name": str(report.source_name or "unknown"),
        "source_observed_at": _utc(observed).isoformat(),
        "created_at": _utc(report.created_at).isoformat(),
        "status": str(report.status or "unknown"),
        "analysis_revision": int(report.analysis_revision or 1),
        "analyzed_revision": int(report.analyzed_revision or 0),
    }


def _manifest_hash(rows: list[tuple[str, str]]) -> str:
    return _canonical_hash({"inputs": [{"report_id": a, "sha256": b} for a, b in sorted(rows)]})


def create_event_replay(
    db: Session,
    *,
    requested_by: str,
    start_at: datetime,
    end_at: datetime,
    max_reports: int = MAX_INPUTS,
    minimum_pair_score: float = 0.62,
    max_distance_km: float = 25.0,
    max_time_gap_hours: float = 72.0,
) -> tuple[EventReplayRun, object]:
    """Freeze eligible inputs and enqueue replay in the same DB transaction."""
    start, end = _utc(start_at), _utc(end_at)
    if start >= end:
        raise ValueError("start_at must be earlier than end_at")
    if (end - start).total_seconds() > MAX_WINDOW_DAYS * 86400:
        raise ValueError(f"Replay window cannot exceed {MAX_WINDOW_DAYS} days")
    limit = max(2, min(MAX_INPUTS, int(max_reports)))
    if not 0.0 <= float(minimum_pair_score) <= 1.0:
        raise ValueError("minimum_pair_score must be between 0 and 1")
    if not 0.1 <= float(max_distance_km) <= 500.0:
        raise ValueError("max_distance_km must be between 0.1 and 500")
    if not 1.0 <= float(max_time_gap_hours) <= 720.0:
        raise ValueError("max_time_gap_hours must be between 1 and 720")

    observed = func.coalesce(Report.source_observed_at, Report.created_at)
    reports = (
        db.query(Report)
        .filter(
            observed >= start,
            observed < end,
            Report.analysis_revision <= Report.analyzed_revision,
            Report.status != "rejected",
            Report.latitude.is_not(None),
            Report.longitude.is_not(None),
        )
        .order_by(observed.asc(), Report.id.asc())
        .limit(limit)
        .all()
    )
    now = utcnow()
    replay_id = str(uuid4())
    params = {
        "max_reports": limit,
        "minimum_pair_score": round(float(minimum_pair_score), 6),
        "max_distance_km": round(float(max_distance_km), 4),
        "max_time_gap_hours": round(float(max_time_gap_hours), 4),
        "semantic_model": None,
        "supervised_pair_model": None,
        "baseline_scope": "overlap_relevant_hypotheses_with_full_membership",
        "deterministic_mode": True,
        "candidate_only": True,
        "human_review_required": True,
    }
    frozen = []
    for report in reports:
        payload = _report_payload(report)
        digest = _canonical_hash(payload)
        frozen.append((report.id, digest, payload))

    # A hypothesis can affect replay comparison only when at least one of its
    # active associations overlaps a frozen replay input. Freeze every active
    # membership row for those hypotheses (including out-of-window reports), but
    # avoid copying unrelated hypotheses from the entire application database.
    input_report_ids = [item[0] for item in frozen]
    overlapping_hypothesis_ids = (
        db.query(EventHypothesisReport.hypothesis_id)
        .filter(
            EventHypothesisReport.report_id.in_(input_report_ids),
            EventHypothesisReport.association_status != "rejected",
        )
        .distinct()
        .all()
        if input_report_ids else []
    )
    relevant_hypothesis_ids = [row[0] for row in overlapping_hypothesis_ids]
    baseline_associations = (
        db.query(EventHypothesisReport)
        .filter(
            EventHypothesisReport.hypothesis_id.in_(relevant_hypothesis_ids),
            EventHypothesisReport.association_status != "rejected",
        )
        .order_by(EventHypothesisReport.hypothesis_id.asc(), EventHypothesisReport.report_id.asc())
        .all()
        if relevant_hypothesis_ids else []
    )
    baseline_ids: dict[str, list[str]] = {}
    for association in baseline_associations:
        baseline_ids.setdefault(association.hypothesis_id, []).append(association.report_id)
    baseline_hypotheses = {
        item.id: item
        for item in db.query(EventHypothesis).filter(EventHypothesis.id.in_(list(baseline_ids))).all()
    } if baseline_ids else {}
    frozen_baseline = []
    for hypothesis_id, report_ids in sorted(baseline_ids.items()):
        hypothesis = baseline_hypotheses.get(hypothesis_id)
        if hypothesis is None:
            continue
        payload = {
            "hypothesis_id": hypothesis_id,
            "hypothesis_status": hypothesis.status,
            "scoring_version": hypothesis.scoring_version,
            "report_ids": sorted(set(report_ids)),
        }
        frozen_baseline.append((payload, _canonical_hash(payload)))

    baseline_manifest = _canonical_hash({
        "baseline": [{"hypothesis_id": item[0]["hypothesis_id"], "sha256": item[1]}
                     for item in frozen_baseline]
    })

    run = EventReplayRun(
        id=replay_id,
        requested_by=requested_by,
        status="queued",
        start_at=start,
        end_at=end,
        parameters=params,
        engine_version=ENGINE_VERSION,
        scoring_version=SCORING_VERSION,
        input_count=len(frozen),
        input_manifest_sha256=_manifest_hash([(item[0], item[1]) for item in frozen]),
        baseline_count=len(frozen_baseline),
        baseline_manifest_sha256=baseline_manifest,
        result_count=0,
        summary={"candidate_only": True, "human_review_required": True},
        created_at=now,
    )
    db.add(run)
    for report_id, digest, payload in frozen:
        db.add(EventReplayInput(
            id=str(uuid4()), run_id=replay_id, report_id=report_id,
            input_sha256=digest, input_data=payload, created_at=now,
        ))
    for payload, digest in frozen_baseline:
        db.add(EventReplayBaseline(
            id=str(uuid4()), run_id=replay_id,
            hypothesis_id=payload["hypothesis_id"],
            hypothesis_status=payload["hypothesis_status"],
            scoring_version=payload["scoring_version"],
            report_ids=payload["report_ids"], baseline_sha256=digest, created_at=now,
        ))
    job = enqueue_event_replay(db, replay_id)
    return run, job


def _comparison(
    report_ids: set[str],
    hypotheses: list[tuple[EventHypothesis, set[str]]],
    *,
    report_index: dict[str, set[int]] | None = None,
    diagnostics: dict | None = None,
) -> tuple[str, str | None, float, dict]:
    if report_index is None:
        candidate_indexes = range(len(hypotheses))
    else:
        candidate_indexes = sorted({
            index for report_id in report_ids
            for index in report_index.get(report_id, ())
        })
    scored = []
    for index in candidate_indexes:
        hypothesis, existing_ids = hypotheses[index]
        intersection = report_ids & existing_ids
        if not intersection:
            continue
        union = report_ids | existing_ids
        jaccard = len(intersection) / len(union) if union else 0.0
        scored.append((jaccard, len(intersection), hypothesis.id, hypothesis, existing_ids))
    if diagnostics is not None:
        diagnostics["baseline_candidates_considered"] = len(candidate_indexes)
    if not scored:
        return "new_candidate", None, 0.0, {
            "matching_method": "report_id_jaccard",
            "overlap_count": 0,
            "baseline_candidates_considered": len(candidate_indexes),
        }
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    score, overlap, _identity, hypothesis, existing_ids = scored[0]
    if report_ids == existing_ids:
        kind = "unchanged"
    elif existing_ids < report_ids:
        kind = "expanded"
    elif report_ids < existing_ids:
        kind = "contracted"
    else:
        kind = "diverged"
    return kind, hypothesis.id, round(score, 6), {
        "matching_method": "report_id_jaccard",
        "overlap_count": overlap,
        "baseline_report_count": len(existing_ids),
        "replay_report_count": len(report_ids),
        "baseline_hypothesis_status": hypothesis.status,
        "baseline_scoring_version": hypothesis.scoring_version,
        "baseline_candidates_considered": len(candidate_indexes),
    }


def execute_event_replay(db: Session, run_id: str) -> dict:
    """Compute replay candidates from frozen inputs without mutating canonical events."""
    run = db.get(EventReplayRun, run_id)
    if run is None:
        return {"ok": False, "reason": "replay_not_found"}
    if run.status == "completed":
        return {"ok": True, "status": "completed", "run_id": run.id, "idempotent_replay": True}

    inputs = (
        db.query(EventReplayInput)
        .filter_by(run_id=run.id)
        .order_by(EventReplayInput.report_id.asc())
        .all()
    )
    manifest_rows = []
    for item in inputs:
        actual = _canonical_hash(item.input_data)
        if actual != item.input_sha256:
            raise ValueError("replay_input_integrity_failure")
        manifest_rows.append((item.report_id, actual))
    if len(inputs) != int(run.input_count) or _manifest_hash(manifest_rows) != run.input_manifest_sha256:
        raise ValueError("replay_manifest_integrity_failure")

    reports = []
    for item in inputs:
        data = dict(item.input_data)
        data["source_observed_at"] = datetime.fromisoformat(data["source_observed_at"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        reports.append(SimpleNamespace(**data))

    params = dict(run.parameters or {})
    clustering_metrics = {}
    clusters = build_candidate_clusters(
        reports,
        minimum_pair_score=float(params["minimum_pair_score"]),
        max_distance_km=float(params["max_distance_km"]),
        max_time_gap_hours=float(params["max_time_gap_hours"]),
        semantic_similarities=None,
        use_supervised_model=False,
        diagnostics=clustering_metrics,
    )

    frozen_baseline = (
        db.query(EventReplayBaseline)
        .filter_by(run_id=run.id)
        .order_by(EventReplayBaseline.hypothesis_id.asc())
        .all()
    )
    baseline_digest_rows = []
    comparison_index = []
    for item in frozen_baseline:
        payload = {
            "hypothesis_id": item.hypothesis_id,
            "hypothesis_status": item.hypothesis_status,
            "scoring_version": item.scoring_version,
            "report_ids": sorted(set(item.report_ids or [])),
        }
        digest = _canonical_hash(payload)
        if digest != item.baseline_sha256:
            raise ValueError("replay_baseline_integrity_failure")
        baseline_digest_rows.append({"hypothesis_id": item.hypothesis_id, "sha256": digest})
        comparison_index.append((
            SimpleNamespace(id=item.hypothesis_id, status=item.hypothesis_status, scoring_version=item.scoring_version),
            set(item.report_ids or []),
        ))
    actual_baseline_manifest = _canonical_hash({"baseline": baseline_digest_rows})
    if len(frozen_baseline) != int(run.baseline_count or 0) or actual_baseline_manifest != run.baseline_manifest_sha256:
        raise ValueError("replay_baseline_manifest_integrity_failure")

    baseline_report_index: dict[str, set[int]] = {}
    for index, (_hypothesis, baseline_report_ids) in enumerate(comparison_index):
        for report_id in baseline_report_ids:
            baseline_report_index.setdefault(report_id, set()).add(index)

    # Delete any partial previous result set before rebuilding deterministically.
    db.query(EventReplayResult).filter_by(run_id=run.id).delete(synchronize_session=False)
    counts = {kind: 0 for kind in ("new_candidate", "unchanged", "expanded", "contracted", "diverged")}
    comparison_metrics = {
        "baseline_hypotheses_frozen": len(comparison_index),
        "naive_baseline_comparisons": len(clusters) * len(comparison_index),
        "indexed_baseline_comparisons": 0,
    }
    for ordinal, cluster in enumerate(clusters, start=1):
        report_ids = set(cluster["report_ids"])
        comparison_diagnostics = {}
        kind, matched_id, overlap_score, compare_detail = _comparison(
            report_ids,
            comparison_index,
            report_index=baseline_report_index,
            diagnostics=comparison_diagnostics,
        )
        comparison_metrics["indexed_baseline_comparisons"] += comparison_diagnostics.get(
            "baseline_candidates_considered", 0
        )
        counts[kind] += 1
        identity = {
            "report_ids": sorted(report_ids),
            "engine_version": run.engine_version,
            "scoring_version": run.scoring_version,
            "parameters": params,
        }
        candidate_key = _canonical_hash(identity)
        pairwise = cluster.get("pairwise", [])
        db.add(EventReplayResult(
            id=str(uuid4()), run_id=run.id, ordinal=ordinal,
            candidate_key=candidate_key, report_ids=sorted(report_ids),
            disaster_type=cluster["disaster_type"],
            centroid_latitude=cluster["centroid_latitude"],
            centroid_longitude=cluster["centroid_longitude"],
            first_observed_at=cluster["first_observed_at"],
            last_observed_at=cluster["last_observed_at"],
            report_count=len(report_ids),
            distinct_source_count=cluster["distinct_source_count"],
            match_score=cluster["match_score"],
            minimum_pair_score=cluster["minimum_pair_score"],
            comparison_kind=kind,
            matched_hypothesis_id=matched_id,
            overlap_score=overlap_score,
            explanation={
                "comparison": compare_detail,
                "source_origins": cluster["source_origins"],
                "pairwise_comparison_count": len(pairwise),
                "minimum_pair_score": params["minimum_pair_score"],
                "maximum_distance_km": params["max_distance_km"],
                "maximum_time_gap_hours": params["max_time_gap_hours"],
                "score_kind": "heuristic_match_score_not_probability",
                "semantic_model": None,
                "supervised_pair_model": None,
                "canonical_state_mutated": False,
            },
            created_at=utcnow(),
        ))

    run.result_count = len(clusters)
    run.summary = {
        "comparison_counts": counts,
        "reports_replayed": len(inputs),
        "candidate_count": len(clusters),
        "candidate_only": True,
        "human_review_required": True,
        "canonical_events_mutated": False,
        "engine_version": run.engine_version,
        "scoring_version": run.scoring_version,
        "input_manifest_sha256": run.input_manifest_sha256,
        "baseline_manifest_sha256": run.baseline_manifest_sha256,
        "baseline_count": run.baseline_count,
        "clustering_metrics": clustering_metrics,
        "comparison_metrics": {
            **comparison_metrics,
            "baseline_comparisons_pruned": max(
                0,
                comparison_metrics["naive_baseline_comparisons"]
                - comparison_metrics["indexed_baseline_comparisons"],
            ),
        },
    }
    run.status = "completed"
    run.failure_code = None
    run.completed_at = utcnow()
    db.flush()
    return {"ok": True, "run_id": run.id, "status": "completed", **run.summary}
