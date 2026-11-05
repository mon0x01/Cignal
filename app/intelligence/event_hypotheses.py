"""Conservative cross-source event hypothesis generation.

Hypotheses are analytical candidates, not verified incidents. Matching scores are
heuristics, not probabilities. Clustering uses complete-link compatibility to avoid
weak transitive chains (A~B and B~C does not imply A~C).
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import combinations
from statistics import mean
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    EventHypothesis,
    EventHypothesisReport,
    EventHypothesisSnapshot,
    Report,
    utcnow,
)
from app.intelligence.analyzer import haversine_km
from app.intelligence.correlation import score_report_pair

SCORING_VERSION = "event-hypothesis-hybrid-v2"
SUPPORTED_DISASTER_TYPES = frozenset({"flood", "earthquake", "fire", "cyclone", "landslide"})
DEFAULT_MINIMUM_PAIR_SCORE = 0.62
DEFAULT_MAX_DISTANCE_KM = 25.0
DEFAULT_MAX_TIME_GAP_HOURS = 72.0


def _event_time(report) -> datetime | None:
    value = getattr(report, "source_observed_at", None) or getattr(report, "created_at", None)
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _source_origin(report) -> str:
    source_type = str(getattr(report, "source_type", "unknown") or "unknown").strip().lower()
    source_name = str(getattr(report, "source_name", "unknown") or "unknown").strip().lower()
    return f"{source_type}:{source_name}"


def _eligible(report) -> bool:
    disaster_type = str(getattr(report, "disaster_type", "unknown") or "unknown").lower()
    latitude = getattr(report, "latitude", None)
    longitude = getattr(report, "longitude", None)
    if disaster_type not in SUPPORTED_DISASTER_TYPES or latitude is None or longitude is None:
        return False
    if not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
        return False
    if _event_time(report) is None or str(getattr(report, "status", "")) == "rejected":
        return False
    current_revision = getattr(report, "analysis_revision", None)
    analyzed_revision = getattr(report, "analyzed_revision", None)
    if current_revision is not None and analyzed_revision is not None:
        if int(analyzed_revision) < int(current_revision):
            return False
    return True


def _pair_result(
    first,
    second,
    *,
    minimum_pair_score: float,
    max_distance_km: float,
    max_time_gap_hours: float,
    semantic_similarity: float | None = None,
    use_supervised_model: bool = True,
) -> dict | None:
    first_type = str(getattr(first, "disaster_type", "unknown") or "unknown").lower()
    second_type = str(getattr(second, "disaster_type", "unknown") or "unknown").lower()
    if first_type != second_type or first_type not in SUPPORTED_DISASTER_TYPES:
        return None

    first_time, second_time = _event_time(first), _event_time(second)
    if first_time is None or second_time is None:
        return None
    age_hours = abs((first_time - second_time).total_seconds()) / 3600.0
    if age_hours > max_time_gap_hours:
        return None

    distance_km = haversine_km(
        float(first.latitude), float(first.longitude),
        float(second.latitude), float(second.longitude),
    )
    if distance_km > max_distance_km:
        return None

    result = score_report_pair(first, second)
    result["reasons"] = list(result.get("reasons", []))
    if semantic_similarity is not None:
        semantic_similarity = max(0.0, min(1.0, float(semantic_similarity)))
        lexical_similarity = float(result["text_similarity"])
        blended_text = 0.35 * lexical_similarity + 0.65 * semantic_similarity
        # Replace the lexical contribution in the existing explainable score;
        # geography, category and time retain their original weights.
        result["score"] = float(result["score"]) + 0.55 * (blended_text - lexical_similarity)
        result["semantic_similarity"] = round(semantic_similarity, 4)
        result["text_match_score"] = round(blended_text, 4)
        if semantic_similarity >= 0.78:
            result["reasons"].append("strong_multilingual_semantic_alignment")
        elif semantic_similarity >= 0.62:
            result["reasons"].append("partial_multilingual_semantic_alignment")
    else:
        result["semantic_similarity"] = None
        result["text_match_score"] = result["text_similarity"]

    supervised = None
    if use_supervised_model:
        from app.intelligence.event_pair_model import score_event_pair_model
        supervised = score_event_pair_model(first, second)
    if supervised is not None:
        # This uncalibrated classifier output is a candidate-ranking score, not a
        # probability. Keep the rule-based score visible in the explanation.
        result["heuristic_score"] = round(float(result["score"]), 4)
        result["trained_model_score"] = round(float(supervised["score"]), 4)
        result["trained_model_version"] = supervised["model_version"]
        result["score"] = round(
            0.55 * float(result["score"]) + 0.45 * float(supervised["score"]), 4
        )
        result["reasons"].append("operator_trained_pair_model_applied")
    result["score"] = round(max(0.0, min(1.0, float(result["score"]))), 4)
    if result["score"] < minimum_pair_score:
        return None
    result["distance_km"] = round(distance_km, 3)
    result["age_hours"] = round(age_hours, 2)
    result["reasons"] = sorted(set(result["reasons"]) | {
        "same_disaster_type", "within_spatial_limit", "within_temporal_limit",
    })
    return result


def build_candidate_clusters(
    reports,
    *,
    minimum_pair_score: float = DEFAULT_MINIMUM_PAIR_SCORE,
    max_distance_km: float = DEFAULT_MAX_DISTANCE_KM,
    max_time_gap_hours: float = DEFAULT_MAX_TIME_GAP_HOURS,
    semantic_similarities: dict[tuple[str, str], float] | None = None,
    use_supervised_model: bool = True,
) -> list[dict]:
    """Build deterministic disjoint complete-link clusters from eligible reports."""
    if not 0.0 <= minimum_pair_score <= 1.0:
        raise ValueError("minimum_pair_score must be between 0 and 1")
    if not 0.1 <= max_distance_km <= 500.0:
        raise ValueError("max_distance_km must be between 0.1 and 500")
    if not 1.0 <= max_time_gap_hours <= 720.0:
        raise ValueError("max_time_gap_hours must be between 1 and 720")

    unique = {}
    for report in reports:
        report_id = str(getattr(report, "id", "") or "")
        if report_id and _eligible(report):
            unique[report_id] = report
    ordered = sorted(unique.values(), key=lambda item: (_event_time(item), str(item.id)))

    clusters: list[list] = []
    pair_cache: dict[tuple[str, str], dict | None] = {}

    def pair(first, second):
        key = tuple(sorted((str(first.id), str(second.id))))
        if key not in pair_cache:
            pair_cache[key] = _pair_result(
                first, second,
                minimum_pair_score=minimum_pair_score,
                max_distance_km=max_distance_km,
                max_time_gap_hours=max_time_gap_hours,
                semantic_similarity=(semantic_similarities or {}).get(key),
                use_supervised_model=use_supervised_model,
            )
        return pair_cache[key]

    for report in ordered:
        compatible = []
        for cluster in clusters:
            scores = [pair(report, member) for member in cluster]
            if scores and all(item is not None for item in scores):
                compatible.append((
                    min(item["score"] for item in scores),
                    mean(item["score"] for item in scores),
                    len(cluster),
                    cluster,
                ))
        if compatible:
            # Prefer strongest worst-case link, then mean link, then larger cluster.
            compatible.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            compatible[0][3].append(report)
        else:
            clusters.append([report])

    output = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        origins = sorted({_source_origin(item) for item in cluster})
        if len(origins) < 2:
            continue
        pairwise = []
        for first, second in combinations(cluster, 2):
            result = pair(first, second)
            if result is not None:
                pairwise.append({
                    **result,
                    "report_a": str(first.id),
                    "report_b": str(second.id),
                })
        if len(pairwise) != (len(cluster) * (len(cluster) - 1)) // 2:
            continue
        cluster = sorted(cluster, key=lambda item: (_event_time(item), str(item.id)))
        output.append({
            "reports": cluster,
            "report_ids": [str(item.id) for item in cluster],
            "anchor_report_id": str(cluster[0].id),
            "disaster_type": str(cluster[0].disaster_type).lower(),
            "source_origins": origins,
            "distinct_source_count": len(origins),
            "match_score": round(mean(item["score"] for item in pairwise), 4),
            "minimum_pair_score": round(min(item["score"] for item in pairwise), 4),
            "pairwise": pairwise,
            "first_observed_at": min(_event_time(item) for item in cluster),
            "last_observed_at": max(_event_time(item) for item in cluster),
            "centroid_latitude": round(mean(float(item.latitude) for item in cluster), 6),
            "centroid_longitude": round(mean(float(item.longitude) for item in cluster), 6),
            "location_text": Counter(
                str(item.location_text).strip() for item in cluster
                if getattr(item, "location_text", None) and str(item.location_text).strip()
            ).most_common(1)[0][0] if any(
                getattr(item, "location_text", None) and str(item.location_text).strip()
                for item in cluster
            ) else None,
        })

    output.sort(key=lambda item: (item["last_observed_at"], item["anchor_report_id"]), reverse=True)
    return output


def _association_summary(report, cluster: dict) -> dict:
    report_id = str(report.id)
    relevant = [
        item for item in cluster["pairwise"]
        if report_id in {item["report_a"], item["report_b"]}
    ]
    if not relevant:
        return {"pair_score": 1.0, "component_scores": {}, "reasons": ["cluster_anchor"]}
    component_names = (
        "text_similarity", "semantic_similarity", "trained_model_score",
        "category_score", "geographic_score", "temporal_score",
    )
    components = {
        name: round(mean(float(item.get(name, 0.0) or 0.0) for item in relevant), 4)
        for name in component_names
    }
    reasons = sorted({reason for item in relevant for reason in item.get("reasons", [])})
    return {
        "pair_score": round(mean(float(item["score"]) for item in relevant), 4),
        "component_scores": components,
        "reasons": reasons,
    }


def _refresh_hypothesis_metrics(db: Session, hypothesis: EventHypothesis, cluster: dict) -> None:
    associations = (
        db.query(EventHypothesisReport)
        .filter(
            EventHypothesisReport.hypothesis_id == hypothesis.id,
            EventHypothesisReport.association_status != "rejected",
        )
        .all()
    )
    active_ids = {item.report_id for item in associations}
    active_reports = (
        db.query(Report).filter(Report.id.in_(active_ids)).all()
        if active_ids else []
    )
    active_pairwise = [
        item for item in cluster["pairwise"]
        if item["report_a"] in active_ids and item["report_b"] in active_ids
    ]
    source_origins = sorted({_source_origin(item) for item in active_reports})

    hypothesis.report_count = len(active_reports)
    hypothesis.distinct_source_count = len(source_origins)
    hypothesis.match_score = (
        round(mean(float(item["score"]) for item in active_pairwise), 4)
        if active_pairwise else 0.0
    )
    if active_reports:
        hypothesis.centroid_latitude = round(
            mean(float(item.latitude) for item in active_reports), 6
        )
        hypothesis.centroid_longitude = round(
            mean(float(item.longitude) for item in active_reports), 6
        )
        event_times = [_event_time(item) for item in active_reports]
        hypothesis.first_observed_at = min(event_times)
        hypothesis.last_observed_at = max(event_times)
        locations = Counter(
            str(item.location_text).strip() for item in active_reports
            if item.location_text and str(item.location_text).strip()
        )
        hypothesis.location_text = locations.most_common(1)[0][0] if locations else None

    component_names = (
        "text_similarity", "semantic_similarity", "trained_model_score",
        "category_score", "geographic_score", "temporal_score",
    )
    component_means = {
        name: round(mean(float(item.get(name, 0.0) or 0.0) for item in active_pairwise), 4)
        for name in component_names
    } if active_pairwise else {}
    previous_explanation = dict(hypothesis.explanation or {})
    previous_explanation.update({
        "source_origins": source_origins,
        "mean_component_scores": component_means,
        "minimum_pair_score_observed": round(
            min(float(item["score"]) for item in active_pairwise), 4
        ) if active_pairwise else 0.0,
        "pairwise_comparison_count": len(active_pairwise),
        "active_report_count": len(active_reports),
    })
    hypothesis.explanation = previous_explanation
    hypothesis.updated_at = utcnow()


def refresh_hypothesis_metrics_from_associations(
    db: Session,
    hypothesis: EventHypothesis,
) -> None:
    """Refresh cached metrics immediately after a human association decision."""
    rows = (
        db.query(EventHypothesisReport, Report)
        .join(Report, Report.id == EventHypothesisReport.report_id)
        .filter(
            EventHypothesisReport.hypothesis_id == hypothesis.id,
            EventHypothesisReport.association_status != "rejected",
        )
        .all()
    )
    reports = [report for _association, report in rows]
    associations = [association for association, _report in rows]
    origins = sorted({_source_origin(report) for report in reports})

    hypothesis.report_count = len(reports)
    hypothesis.distinct_source_count = len(origins)
    if reports:
        hypothesis.centroid_latitude = round(
            mean(float(report.latitude) for report in reports), 6
        )
        hypothesis.centroid_longitude = round(
            mean(float(report.longitude) for report in reports), 6
        )
        event_times = [_event_time(report) for report in reports]
        hypothesis.first_observed_at = min(event_times)
        hypothesis.last_observed_at = max(event_times)
        locations = Counter(
            str(report.location_text).strip() for report in reports
            if report.location_text and str(report.location_text).strip()
        )
        hypothesis.location_text = locations.most_common(1)[0][0] if locations else None

    explanation = dict(hypothesis.explanation or {})
    minimum_pair_score = float(
        explanation.get("minimum_pair_score", DEFAULT_MINIMUM_PAIR_SCORE)
    )
    max_distance_km = float(
        explanation.get("maximum_distance_km", DEFAULT_MAX_DISTANCE_KM)
    )
    max_time_gap_hours = float(
        explanation.get("maximum_time_gap_hours", DEFAULT_MAX_TIME_GAP_HOURS)
    )
    semantic_version = explanation.get("semantic_model_version")
    from app.intelligence.semantic import cached_similarity_map

    semantic_map = cached_similarity_map(db, reports, semantic_version)
    pairwise = [
        result
        for first, second in combinations(reports, 2)
        if (result := _pair_result(
            first,
            second,
            minimum_pair_score=minimum_pair_score,
            max_distance_km=max_distance_km,
            max_time_gap_hours=max_time_gap_hours,
            semantic_similarity=semantic_map.get(tuple(sorted((str(first.id), str(second.id))))),
        )) is not None
    ]
    component_names = (
        "text_similarity", "semantic_similarity", "trained_model_score",
        "category_score", "geographic_score", "temporal_score",
    )
    hypothesis.match_score = (
        round(mean(float(item["score"]) for item in pairwise), 4)
        if pairwise else 0.0
    )
    explanation.update({
        "source_origins": origins,
        "mean_component_scores": {
            name: round(mean(float(item.get(name, 0.0) or 0.0) for item in pairwise), 4)
            for name in component_names
        } if pairwise else {},
        "minimum_pair_score_observed": round(
            min(float(item["score"]) for item in pairwise), 4
        ) if pairwise else 0.0,
        "pairwise_comparison_count": len(pairwise),
        "active_report_count": len(reports),
        "active_association_count": len(associations),
        "incompatible_pair_count": (len(reports) * (len(reports) - 1)) // 2 - len(pairwise),
    })
    hypothesis.explanation = explanation
    hypothesis.updated_at = utcnow()


def _record_hypothesis_snapshot(
    db: Session,
    hypothesis: EventHypothesis,
    *,
    discovery_run_id: str | None,
    change_kind: str,
    semantic_status: dict,
) -> bool:
    associations = (
        db.query(EventHypothesisReport)
        .filter(
            EventHypothesisReport.hypothesis_id == hypothesis.id,
            EventHypothesisReport.association_status != "rejected",
        )
        .order_by(EventHypothesisReport.report_id.asc())
        .all()
    )
    report_ids = [row.report_id for row in associations]
    association_state = [
        {
            "report_id": row.report_id,
            "status": row.association_status,
            "relationship_type": row.relationship_type,
        }
        for row in associations
    ]
    snapshot_data = {
        "report_ids": report_ids,
        "associations": association_state,
        "source_origins": list((hypothesis.explanation or {}).get("source_origins", [])),
        "scoring_version": hypothesis.scoring_version,
        "hypothesis_status": hypothesis.status,
        "semantic": semantic_status,
        "score_kind": "heuristic_match_score_not_probability",
    }
    identity = {
        "reports": report_ids,
        "associations": association_state,
        "score": round(float(hypothesis.match_score), 5),
        "sources": snapshot_data["source_origins"],
        "last_observed_at": hypothesis.last_observed_at.isoformat(),
        "scoring_version": hypothesis.scoring_version,
        "hypothesis_status": hypothesis.status,
        "semantic_model_version": semantic_status.get("model_version"),
    }
    snapshot_key = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = db.query(EventHypothesisSnapshot.id).filter_by(
        hypothesis_id=hypothesis.id, snapshot_key=snapshot_key
    ).first()
    if existing:
        return False
    db.add(EventHypothesisSnapshot(
        id=str(uuid4()),
        hypothesis_id=hypothesis.id,
        discovery_run_id=discovery_run_id,
        snapshot_key=snapshot_key,
        change_kind=change_kind,
        report_count=hypothesis.report_count,
        distinct_source_count=hypothesis.distinct_source_count,
        match_score=hypothesis.match_score,
        centroid_latitude=hypothesis.centroid_latitude,
        centroid_longitude=hypothesis.centroid_longitude,
        first_observed_at=hypothesis.first_observed_at,
        last_observed_at=hypothesis.last_observed_at,
        snapshot_data=snapshot_data,
        created_at=utcnow(),
    ))
    db.flush()
    return True


def record_hypothesis_review_snapshot(
    db: Session,
    hypothesis: EventHypothesis,
    *,
    change_kind: str,
) -> bool:
    """Append a snapshot for explicit analyst review/status changes."""
    explanation = dict(hypothesis.explanation or {})
    semantic_status = dict(explanation.get("semantic") or {})
    semantic_status["supervised_pair_model"] = explanation.get(
        "supervised_pair_model", {}
    )
    return _record_hypothesis_snapshot(
        db,
        hypothesis,
        discovery_run_id=None,
        change_kind=change_kind,
        semantic_status=semantic_status,
    )


def _event_key(anchor_report_id: str) -> str:
    return sha256(f"{SCORING_VERSION}:{anchor_report_id}".encode()).hexdigest()


def discover_event_hypotheses(
    db: Session,
    *,
    requested_by: str | None = None,
    discovery_run_id: str | None = None,
    window_hours: int = 72,
    max_reports: int = 300,
    minimum_pair_score: float = DEFAULT_MINIMUM_PAIR_SCORE,
    max_distance_km: float = DEFAULT_MAX_DISTANCE_KM,
) -> dict:
    """Persist new candidate hypotheses without mutating report or incident truth."""
    if not 1 <= window_hours <= 168:
        raise ValueError("window_hours must be between 1 and 168")
    if not 2 <= max_reports <= 500:
        raise ValueError("max_reports must be between 2 and 500")

    cutoff = utcnow() - timedelta(hours=window_hours)
    observed_expr = func.coalesce(Report.source_observed_at, Report.created_at)
    reports = (
        db.query(Report)
        .filter(
            observed_expr >= cutoff,
            Report.analysis_revision <= Report.analyzed_revision,
            Report.status != "rejected",
            Report.latitude.is_not(None),
            Report.longitude.is_not(None),
        )
        # The cap must favour fresh observations. Candidate clustering sorts
        # this bounded set chronologically itself; selecting oldest-first here
        # can starve newly arriving reports when a busy region exceeds max_reports.
        .order_by(observed_expr.desc(), Report.id.asc())
        .limit(max_reports)
        .all()
    )
    from app.intelligence.semantic import embed_reports

    embedding_batch = embed_reports(db, reports)
    from app.intelligence.event_pair_model import event_pair_model_status

    supervised_status = event_pair_model_status()
    semantic_status = {
        "status": embedding_batch.status,
        "model_version": embedding_batch.model_version,
        "cache_hits": embedding_batch.cache_hits,
        "encoded_count": embedding_batch.encoded_count,
        "dimensions": embedding_batch.dimensions,
        "reason": embedding_batch.reason,
        "enabled": embedding_batch.status == "available",
        "interpretation": "Cosine similarity from a configured multilingual SentenceTransformer; not a probability.",
        "supervised_pair_model": supervised_status,
    }
    clusters = build_candidate_clusters(
        reports,
        minimum_pair_score=minimum_pair_score,
        max_distance_km=max_distance_km,
        max_time_gap_hours=float(window_hours),
        semantic_similarities=embedding_batch.similarities,
    )

    created = updated = skipped = snapshots_written = 0
    hypothesis_ids = []
    for cluster in clusters:
        report_ids = cluster["report_ids"]
        anchor_id = cluster["anchor_report_id"]
        existing = db.query(EventHypothesis).filter_by(anchor_report_id=anchor_id).one_or_none()
        previous_report_count = int(existing.report_count or 0) if existing is not None else 0
        previous_score = float(existing.match_score or 0.0) if existing is not None else 0.0

        overlapping = (
            db.query(EventHypothesis.id)
            .join(EventHypothesisReport, EventHypothesisReport.hypothesis_id == EventHypothesis.id)
            .filter(
                EventHypothesisReport.report_id.in_(report_ids),
                EventHypothesis.status.in_(("candidate", "investigating")),
            )
            .distinct()
            .all()
        )
        overlapping_ids = {row[0] for row in overlapping}
        if existing is not None:
            overlapping_ids.discard(existing.id)
            if existing.status != "candidate" or overlapping_ids:
                skipped += 1
                continue
            old_rows = db.query(EventHypothesisReport).filter_by(hypothesis_id=existing.id).all()
            old_ids = {row.report_id for row in old_rows if row.association_status != "rejected"}
            if not old_ids.issubset(set(report_ids)):
                skipped += 1
                continue
            for report in cluster["reports"]:
                association = next((row for row in old_rows if row.report_id == report.id), None)
                if association is None:
                    summary = _association_summary(report, cluster)
                    db.add(EventHypothesisReport(
                        id=str(uuid4()), hypothesis_id=existing.id, report_id=report.id,
                        association_status="proposed", relationship_type="supports",
                        pair_score=summary["pair_score"], component_scores=summary["component_scores"],
                        reasons=summary["reasons"],
                    ))
                elif association.association_status == "rejected":
                    continue
            hypothesis = existing
            updated += 1
        else:
            if overlapping_ids:
                skipped += 1
                continue
            hypothesis_id = str(uuid4())
            key = _event_key(anchor_id)
            location = cluster["location_text"]
            title_location = f" — {location[:70]}" if location else ""
            title = f"{cluster['disaster_type'].title()} event candidate{title_location}"[:180]
            summary = (
                f"Potential {cluster['disaster_type']} event supported by {len(report_ids)} "
                f"reports from {cluster['distinct_source_count']} distinct source origins. "
                "This is an unverified analytical hypothesis and requires analyst review."
            )
            pairwise = cluster["pairwise"]
            component_names = (
                "text_similarity",
                "semantic_similarity",
                "category_score",
                "geographic_score",
                "temporal_score",
            )
            component_means = {
                name: round(mean(float(item.get(name, 0.0) or 0.0) for item in pairwise), 4)
                for name in component_names
            }
            hypothesis = EventHypothesis(
                id=hypothesis_id,
                event_key=key,
                anchor_report_id=anchor_id,
                title=title,
                summary=summary,
                disaster_type=cluster["disaster_type"],
                status="candidate",
                centroid_latitude=cluster["centroid_latitude"],
                centroid_longitude=cluster["centroid_longitude"],
                location_text=location,
                first_observed_at=cluster["first_observed_at"],
                last_observed_at=cluster["last_observed_at"],
                report_count=len(report_ids),
                distinct_source_count=cluster["distinct_source_count"],
                match_score=cluster["match_score"],
                scoring_version=SCORING_VERSION,
                explanation={
                    "score_kind": "heuristic_match_score_not_probability",
                    "scoring_version": SCORING_VERSION,
                    "clustering_method": "greedy_complete_link",
                    "minimum_pair_score": minimum_pair_score,
                    "maximum_distance_km": max_distance_km,
                    "maximum_time_gap_hours": window_hours,
                    "mean_component_scores": component_means,
                    "minimum_pair_score_observed": cluster["minimum_pair_score"],
                    "pairwise_comparison_count": len(pairwise),
                    "active_report_count": len(report_ids),
                    "source_origins": cluster["source_origins"],
                    "semantic": semantic_status,
                    "semantic_model_version": embedding_batch.model_version,
                    "human_review_required": True,
                    "automatic_incident_creation": False,
                },
                created_by=requested_by,
            )
            db.add(hypothesis)
            for report in cluster["reports"]:
                summary = _association_summary(report, cluster)
                db.add(EventHypothesisReport(
                    id=str(uuid4()), hypothesis_id=hypothesis_id, report_id=report.id,
                    association_status="proposed", relationship_type="supports",
                    pair_score=summary["pair_score"], component_scores=summary["component_scores"],
                    reasons=summary["reasons"],
                ))
            created += 1

        # Existing hypotheses only grow when the current complete-link cluster is
        # a superset. Human decisions are never overwritten by automatic discovery.
        _refresh_hypothesis_metrics(db, hypothesis, cluster)
        hypothesis.scoring_version = SCORING_VERSION
        updated_explanation = dict(hypothesis.explanation or {})
        updated_explanation.update({
            "semantic": semantic_status,
            "semantic_model_version": embedding_batch.model_version,
            "score_kind": "heuristic_match_score_not_probability",
        })
        hypothesis.explanation = updated_explanation
        db.flush()
        if previous_report_count == 0:
            change_kind = "created"
        elif hypothesis.report_count > previous_report_count:
            change_kind = "expanded"
        elif abs(float(hypothesis.match_score) - previous_score) > 1e-6:
            change_kind = "rescored"
        else:
            change_kind = "observed"
        if _record_hypothesis_snapshot(
            db,
            hypothesis,
            discovery_run_id=discovery_run_id,
            change_kind=change_kind,
            semantic_status=semantic_status,
        ):
            snapshots_written += 1
        hypothesis_ids.append(hypothesis.id)

    db.flush()
    return {
        "reports_scanned": len(reports),
        "clusters_found": len(clusters),
        "hypotheses_created": created,
        "hypotheses_updated": updated,
        "clusters_skipped": skipped,
        "hypothesis_ids": hypothesis_ids,
        "hypothesis_snapshots_written": snapshots_written,
        "semantic": semantic_status,
        "supervised_pair_model": supervised_status,
        "scoring_version": SCORING_VERSION,
    }
