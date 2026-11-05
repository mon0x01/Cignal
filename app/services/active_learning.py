"""Leakage-aware, analyst-only candidate mining for event-pair labels."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    EventPairCandidate,
    EventPairLabel,
    PairCandidateMiningRun,
    Report,
    ResearchDataset,
)
from app.intelligence.correlation import score_report_pair
from app.intelligence.event_pair_model import score_event_pair_model
from app.services.research_datasets import pair_fingerprint


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def mine_event_pair_candidates(
    db: Session,
    *,
    run_id: str,
    max_reports: int = 1200,
    window_hours: int = 168,
    max_distance_km: float = 250.0,
    max_pairs_scored: int = 100_000,
    top_candidates: int = 2000,
) -> dict:
    """Create ranked unlabelled candidates; never assigns same/different-event labels."""
    run = db.get(PairCandidateMiningRun, run_id)
    if run is None:
        return {"ok": False, "reason": "mining_run_not_found"}
    if run.status == "completed":
        return {"ok": True, "status": "completed", "run_id": run_id}
    run.status = "running"
    db.commit()
    try:
        dataset = db.get(ResearchDataset, run.dataset_id)
        if dataset is None or dataset.status != "active":
            raise ValueError("dataset_missing_or_inactive")
        params = dict(run.parameters or {})
        max_reports = max(20, min(2000, int(params.get("max_reports", max_reports))))
        window_hours = max(1, min(720, int(params.get("window_hours", window_hours))))
        max_distance_km = max(1.0, min(2000.0, float(params.get("max_distance_km", max_distance_km))))
        max_pairs_scored = max(100, min(250_000, int(params.get("max_pairs_scored", max_pairs_scored))))
        top_candidates = max(1, min(5000, int(params.get("top_candidates", top_candidates))))
        allowed_sources = set(dataset.source_types or [])
        query = db.query(Report)
        if "*" not in allowed_sources:
            query = query.filter(Report.source_type.in_(allowed_sources))
        reports = query.order_by(Report.source_observed_at.desc().nullslast(), Report.created_at.desc()).limit(max_reports).all()
        reports.sort(key=lambda row: _aware(row.source_observed_at or row.created_at) or datetime.min.replace(tzinfo=UTC))
        run.reports_considered = len(reports)
        labelled = {item[0] for item in db.query(EventPairLabel.pair_fingerprint).filter_by(dataset_id=dataset.id).all()}
        existing = {
            item[0]: item[1]
            for item in db.query(EventPairCandidate.pair_fingerprint, EventPairCandidate.status)
            .filter_by(dataset_id=dataset.id).all()
        }
        candidates: list[dict] = []
        scored = 0
        lower_bound = 0
        for right_index, right in enumerate(reports):
            right_time = _aware(right.source_observed_at or right.created_at)
            if right_time is None:
                continue
            while lower_bound < right_index:
                left_time = _aware(reports[lower_bound].source_observed_at or reports[lower_bound].created_at)
                if left_time is not None and (right_time - left_time).total_seconds() <= window_hours * 3600:
                    break
                lower_bound += 1
            for left in reports[lower_bound:right_index]:
                if left.id == right.id:
                    continue
                left_type = (left.disaster_type or "unknown").lower()
                right_type = (right.disaster_type or "unknown").lower()
                if left_type not in {"unknown", "other"} and right_type not in {"unknown", "other"} and left_type != right_type:
                    continue
                left_time = _aware(left.source_observed_at or left.created_at)
                age_hours = abs((right_time - left_time).total_seconds()) / 3600 if left_time else None
                if age_hours is None or age_hours > window_hours:
                    continue
                if scored >= max_pairs_scored:
                    break
                scored += 1
                result = score_report_pair(left, right)
                distance = result.get("distance_km")
                if distance is not None and distance > max_distance_km:
                    # Keep geographically conflicting lexical near-matches as hard-negative candidates.
                    if result.get("text_similarity", 0.0) < 0.55:
                        continue
                if result.get("text_similarity", 0.0) < 0.12 and result["score"] < 0.30:
                    continue
                fingerprint = pair_fingerprint(*sorted((left.id, right.id)))
                if fingerprint in labelled:
                    continue
                if existing.get(fingerprint) in {"pending", "labelled", "dismissed"}:
                    continue
                score = float(result["score"])
                uncertainty = 1.0 - min(1.0, abs(score - 0.5) * 2.0)
                model_result = score_event_pair_model(left, right)
                supervised_score = float(model_result["score"]) if model_result else None
                model_uncertainty = (
                    1.0 - min(1.0, abs(supervised_score - 0.5) * 2.0)
                    if supervised_score is not None else uncertainty
                )
                hard_negative_hint = bool(
                    result.get("text_similarity", 0.0) >= 0.55
                    and ((distance is not None and distance > max_distance_km * 0.5)
                         or age_hours > window_hours * 0.5)
                )
                selection = min(
                    1.0,
                    0.42 * uncertainty + 0.33 * model_uncertainty
                    + (0.25 if hard_negative_hint else 0.0),
                )
                candidates.append({
                    "left": left, "right": right, "fingerprint": fingerprint,
                    "heuristic_score": score, "supervised_score": supervised_score,
                    "model_version": model_result.get("model_version") if model_result else None,
                    "selection_score": selection,
                    "reasons": {
                        **result, "hard_negative_hint": hard_negative_hint,
                        "source_diversity": left.source_type != right.source_type,
                        "policy": "unlabelled_candidate_only_human_review_required",
                    },
                })
            if scored >= max_pairs_scored:
                break
        candidates.sort(key=lambda item: (item["selection_score"], item["heuristic_score"]), reverse=True)
        created = 0
        for item in candidates[:top_candidates]:
            candidate = EventPairCandidate(
                id=str(uuid4()), dataset_id=dataset.id, mining_run_id=run.id,
                report_a_id=item["left"].id, report_b_id=item["right"].id,
                pair_fingerprint=item["fingerprint"], heuristic_score=item["heuristic_score"],
                supervised_score=item["supervised_score"], selection_score=item["selection_score"],
                model_version=item["model_version"],
                candidate_reasons=item["reasons"], status="pending",
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            try:
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
                created += 1
            except IntegrityError:
                continue
        run.status = "completed"
        run.pairs_scored = min(scored, max_pairs_scored)
        run.candidates_created = created
        run.candidates_existing = max(0, len(candidates) - created)
        run.completed_at = datetime.now(UTC)
        db.commit()
        return {
            "ok": True, "status": run.status, "run_id": run.id,
            "reports_considered": len(reports), "pairs_scored": run.pairs_scored,
            "candidates_created": created, "candidate_limit": top_candidates,
            "hard_negative_hints": sum(1 for item in candidates[:top_candidates] if item["reasons"]["hard_negative_hint"]),
        }
    except Exception as exc:
        db.rollback()
        failed = db.get(PairCandidateMiningRun, run_id)
        if failed is not None:
            failed.status = "failed"
            failed.failure_code = type(exc).__name__[:80]
            failed.completed_at = datetime.now(UTC)
            db.commit()
        raise
