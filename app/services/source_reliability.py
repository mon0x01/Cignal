"""Auditable source operations and analyst-review profile aggregation.

Operational success and analyst acceptance are kept separate. Neither metric is
represented as a statement that the source's reports are objectively truthful.
"""
from __future__ import annotations

import re

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.models import (
    EventHypothesisReport,
    IntegrationRun,
    Report,
    SourceReliabilityProfile,
    utcnow,
)

METHOD_VERSION = "source-profile-beta-wilson-v1"
PRIOR_ALPHA = 2.0
PRIOR_BETA = 2.0


def source_origin(source_type: str | None, source_name: str | None) -> str:
    normalized_type = re.sub(r"[^a-z0-9_.-]+", "_", (source_type or "unknown").lower()).strip("_")
    normalized_name = re.sub(r"\s+", " ", (source_name or "unknown").strip().lower())
    # Source names are labels, not endpoint URLs. Strip query/fragment material if
    # a provider adapter accidentally supplies a URL as its display name.
    normalized_name = normalized_name.split("?", 1)[0].split("#", 1)[0]
    return f"{normalized_type[:24]}:{normalized_name[:100]}"


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = min(1.0, max(0.0, successes / total))
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * ((p * (1.0 - p) / total + z2 / (4.0 * total * total)) ** 0.5)
    return round(max(0.0, (centre - margin) / denominator), 4)


def confidence_band(reviewed: int) -> str:
    if reviewed < 5:
        return "insufficient"
    if reviewed < 20:
        return "low"
    if reviewed < 50:
        return "moderate"
    return "higher_sample"


def rebuild_source_reliability_profiles(db: Session) -> dict:
    """Recompute materialized profiles from ingestion runs and human decisions."""
    aggregates: dict[str, dict] = {}

    run_rows = (
        db.query(
            IntegrationRun.source_type,
            IntegrationRun.source_name,
            func.count(IntegrationRun.id),
            func.sum(case((IntegrationRun.status == "success", 1), else_=0)),
            func.sum(case((IntegrationRun.status == "partial", 1), else_=0)),
            func.sum(case((IntegrationRun.status == "failed", 1), else_=0)),
            func.coalesce(func.sum(IntegrationRun.records_fetched), 0),
            func.coalesce(func.sum(IntegrationRun.records_inserted), 0),
            func.coalesce(func.sum(IntegrationRun.records_rejected), 0),
        )
        .group_by(IntegrationRun.source_type, IntegrationRun.source_name)
        .all()
    )
    for row in run_rows:
        origin = source_origin(row[0], row[1])
        aggregates[origin] = {
            "source_type": str(row[0] or "unknown")[:24],
            "source_name": str(row[1] or "unknown")[:100],
            "ingestion_runs": int(row[2] or 0),
            "successful_runs": int(row[3] or 0),
            "partial_runs": int(row[4] or 0),
            "failed_runs": int(row[5] or 0),
            "records_fetched": int(row[6] or 0),
            "records_inserted": int(row[7] or 0),
            "records_rejected": int(row[8] or 0),
        }

    review_rows = (
        db.query(
            Report.source_type,
            Report.source_name,
            EventHypothesisReport.association_status,
            func.count(EventHypothesisReport.id),
            func.max(EventHypothesisReport.reviewed_at),
        )
        .join(Report, Report.id == EventHypothesisReport.report_id)
        .filter(EventHypothesisReport.association_status.in_(("confirmed", "rejected")))
        .group_by(
            Report.source_type,
            Report.source_name,
            EventHypothesisReport.association_status,
        )
        .all()
    )
    for row in review_rows:
        origin = source_origin(row[0], row[1])
        profile = aggregates.setdefault(origin, {
            "source_type": str(row[0] or "unknown")[:24],
            "source_name": str(row[1] or "unknown")[:100],
            "ingestion_runs": 0,
            "successful_runs": 0,
            "partial_runs": 0,
            "failed_runs": 0,
            "records_fetched": 0,
            "records_inserted": 0,
            "records_rejected": 0,
        })
        profile.setdefault("reviewed_associations", 0)
        profile.setdefault("confirmed_associations", 0)
        profile.setdefault("rejected_associations", 0)
        profile["reviewed_associations"] += int(row[3] or 0)
        if row[2] == "confirmed":
            profile["confirmed_associations"] += int(row[3] or 0)
        else:
            profile["rejected_associations"] += int(row[3] or 0)
        prior = profile.get("last_reviewed_at")
        if row[4] is not None and (prior is None or row[4] > prior):
            profile["last_reviewed_at"] = row[4]

    refreshed = 0
    now = utcnow()
    for origin, values in aggregates.items():
        reviewed = int(values.get("reviewed_associations", 0))
        confirmed = int(values.get("confirmed_associations", 0))
        rejected = int(values.get("rejected_associations", 0))
        runs = int(values.get("ingestion_runs", 0))
        profile = db.get(SourceReliabilityProfile, origin)
        if profile is None:
            profile = SourceReliabilityProfile(source_origin=origin)
            db.add(profile)
        for key in (
            "source_type", "source_name", "ingestion_runs", "successful_runs",
            "partial_runs", "failed_runs", "records_fetched", "records_inserted",
            "records_rejected",
        ):
            setattr(profile, key, values.get(key, 0 if key != "source_type" else "unknown"))
        profile.reviewed_associations = reviewed
        profile.confirmed_associations = confirmed
        profile.rejected_associations = rejected
        profile.operational_success_rate = round(
            int(values.get("successful_runs", 0)) / runs, 4
        ) if runs else None
        profile.review_acceptance_posterior_mean = round(
            (confirmed + PRIOR_ALPHA) / (reviewed + PRIOR_ALPHA + PRIOR_BETA), 4
        ) if reviewed else None
        profile.review_acceptance_wilson_lower = wilson_lower_bound(confirmed, reviewed)
        profile.review_confidence = confidence_band(reviewed)
        profile.method_version = METHOD_VERSION
        profile.last_reviewed_at = values.get("last_reviewed_at")
        profile.updated_at = now
        refreshed += 1

    db.flush()
    return {"profiles_refreshed": refreshed, "method_version": METHOD_VERSION}


def source_reliability_dict(profile: SourceReliabilityProfile) -> dict:
    return {
        "source_origin": profile.source_origin,
        "source_type": profile.source_type,
        "source_name": profile.source_name,
        "operational": {
            "runs": profile.ingestion_runs,
            "successful_runs": profile.successful_runs,
            "partial_runs": profile.partial_runs,
            "failed_runs": profile.failed_runs,
            "success_rate": profile.operational_success_rate,
            "records_fetched": profile.records_fetched,
            "records_inserted": profile.records_inserted,
            "records_rejected": profile.records_rejected,
        },
        "analyst_review": {
            "reviewed_associations": profile.reviewed_associations,
            "confirmed_associations": profile.confirmed_associations,
            "rejected_associations": profile.rejected_associations,
            "posterior_acceptance_mean": profile.review_acceptance_posterior_mean,
            "wilson_95_lower_bound": profile.review_acceptance_wilson_lower,
            "confidence_band": profile.review_confidence,
            "last_reviewed_at": profile.last_reviewed_at.isoformat() if profile.last_reviewed_at else None,
        },
        "interpretation": (
            "Operational success measures provider run outcomes. Analyst acceptance measures "
            "human-reviewed report-to-event associations. Neither metric proves source truthfulness."
        ),
        "method_version": profile.method_version,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }
