from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.integrations.source_catalog import build_report_source_metadata, source_descriptor_payload
from app.services.research_datasets import (
    ResearchDatasetError,
    build_pair_row,
    pair_fingerprint,
    report_content_fingerprint,
    report_source_metadata_fingerprint,
)


def _report(report_id: str, source_type: str):
    return SimpleNamespace(
        id=report_id,
        title=f"Flood report {report_id}",
        description=f"Flood waters rising in settlement {report_id}; road access affected.",
        disaster_type="flood",
        latitude=23.0,
        longitude=72.5,
        source_observed_at=datetime(2026, 11, 5, tzinfo=UTC),
        created_at=datetime(2026, 11, 5, tzinfo=UTC),
        language="en",
        source_type=source_type,
        source_name=source_type,
        source_record_id=f"source-{report_id}",
        source_metadata=build_report_source_metadata(source_type, {"provider_record_id": report_id}),
    )


def _label(first, second):
    return SimpleNamespace(
        id="label-1",
        pair_fingerprint=pair_fingerprint(first.id, second.id),
        split_group="flood-event-1",
        label="same_event",
        label_method="analyst_review",
        review_status="approved",
        privacy_review_status="passed",
        reviewed_at=datetime(2026, 11, 5, tzinfo=UTC),
        provenance_snapshot={
            "report_a": {
                "report_id": first.id,
                "content_sha256": report_content_fingerprint(first),
                "source_metadata_sha256": report_source_metadata_fingerprint(first),
            },
            "report_b": {
                "report_id": second.id,
                "content_sha256": report_content_fingerprint(second),
                "source_metadata_sha256": report_source_metadata_fingerprint(second),
            },
        },
    )


def test_citizen_source_requires_consent_scope_review():
    descriptor = source_descriptor_payload("citizen")
    assert descriptor["license_status"] == "consent_scope_review_required"
    assert descriptor["retention_policy"] == "purpose_limited_user_submitted_content"


def test_research_pair_provenance_includes_provider_governance_snapshot():
    first, second = _report("report-a", "nasa_eonet"), _report("report-b", "sachet_cap")
    label = _label(first, second)
    _row, provenance, _redactions = build_pair_row(label, first, second, coordinate_precision=2)
    assert provenance["report_a"]["source_governance"]["license_status"] == "source_dependent_metadata"
    assert provenance["report_b"]["source_governance"]["license_status"] == "official_feed_terms_review_required"
    assert provenance["report_a"]["source_metadata_sha256"] == report_source_metadata_fingerprint(first)


def test_research_pair_rejects_source_governance_metadata_drift():
    first, second = _report("report-a", "nasa_eonet"), _report("report-b", "sachet_cap")
    label = _label(first, second)
    first.source_metadata["license_status"] = "operator_review_required"
    with pytest.raises(ResearchDatasetError, match="governance metadata changed"):
        build_pair_row(label, first, second, coordinate_precision=2)
