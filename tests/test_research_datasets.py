from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.intelligence.event_pair_training import (
    EventPairDatasetError,
    _training_texts,
    load_event_pair_jsonl,
)
from app.services.research_datasets import (
    ResearchDatasetError,
    canonical_jsonl,
    pair_fingerprint,
    prepare_dataset_snapshot,
    redact_obvious_identifiers,
    report_content_fingerprint,
)


def _report(report_id: str, suffix: str, *, source_type: str = "usgs"):
    return SimpleNamespace(
        id=report_id,
        title=f"Flood report {suffix}",
        description=f"Flood water observed near settlement {suffix}. Residents report rising water and road access constraints.",
        disaster_type="flood",
        latitude=23.0,
        longitude=72.5,
        source_observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        language="en",
        source_type=source_type,
        source_name=source_type.upper(),
        source_record_id=f"provider-{suffix}",
    )


def _labels(count: int = 16):
    rows = []
    for index in range(count):
        group = f"disaster-{index // 4 + 1}"
        same = index % 2 == 0
        source_a = "usgs" if index % 2 == 0 else "gdacs"
        source_b = "gdacs" if source_a == "usgs" else "imd"
        first = _report(f"report-a-{index}", f"a-{index}", source_type=source_a)
        second = _report(f"report-b-{index}", f"b-{index}", source_type=source_b)
        label = SimpleNamespace(
            id=f"label-{index}",
            pair_fingerprint=pair_fingerprint(first.id, second.id),
            split_group=group,
            label="same_event" if same else "different_event",
            label_method="analyst_review",
            review_status="approved",
            privacy_review_status="passed",
            reviewed_at=datetime(2026, 2, 1, tzinfo=UTC),
            provenance_snapshot={
                "report_a": {
                    "report_id": first.id,
                    "content_sha256": report_content_fingerprint(first),
                },
                "report_b": {
                    "report_id": second.id,
                    "content_sha256": report_content_fingerprint(second),
                },
            },
        )
        rows.append((label, first, second))
    return rows


def test_pair_label_schema_rejects_obvious_personal_identifiers_in_split_group():
    from app.api.schemas import EventPairLabelCreate

    base = {
        "report_a_id": "11111111-1111-4111-8111-111111111111",
        "report_b_id": "22222222-2222-4222-8222-222222222222",
        "label": "same_event",
        "rationale": "Two reviewed reports describe one event.",
        "label_method": "analyst_review",
    }
    for group in ("contact responder@example.org", "incident 9876543210"):
        with pytest.raises(ValueError, match="personal email or phone"):
            EventPairLabelCreate(**base, split_group=group)


def test_pair_fingerprint_is_order_independent_and_rejects_self_pair():
    assert pair_fingerprint("report-a", "report-b") == pair_fingerprint("report-b", "report-a")
    with pytest.raises(ResearchDatasetError, match="itself"):
        pair_fingerprint("same", "same")


def test_redactor_removes_obvious_email_and_indian_mobile_patterns():
    cleaned, counts = redact_obvious_identifiers(
        "Contact flooddesk@example.org or call +91 9876543210 for details."
    )
    assert "flooddesk@example.org" not in cleaned
    assert "9876543210" not in cleaned
    assert counts == {"email": 1, "phone": 1}


def test_prepare_snapshot_is_deterministic_and_group_disjoint_ready():
    result = prepare_dataset_snapshot(_labels())
    assert result["record_count"] == 16
    assert result["same_event_count"] == 8
    assert result["different_event_count"] == 8
    assert result["split_group_count"] == 4
    assert result["positive_group_count"] == 4
    assert result["negative_group_count"] == 4
    assert result["mixed_group_count"] == 4
    assert result["content_sha256"] == __import__("hashlib").sha256(
        canonical_jsonl(result["records"])
    ).hexdigest()
    assert result["content_sha256"] == prepare_dataset_snapshot(_labels())["content_sha256"]


def test_prepare_snapshot_rejects_group_leakage_for_duplicate_text_pair():
    rows = _labels()
    duplicate_label, report_a, report_b = rows[0]
    other = SimpleNamespace(**vars(duplicate_label))
    other.id = "label-duplicate"
    other.split_group = "disaster-999"
    with pytest.raises(ResearchDatasetError, match="multiple split_group"):
        prepare_dataset_snapshot(rows + [(other, report_a, report_b)])


def test_prepare_snapshot_requires_both_labels_in_multiple_groups():
    rows = _labels()
    only_positive = []
    for index, (label, report_a, report_b) in enumerate(rows):
        clone = SimpleNamespace(**vars(label))
        clone.label = "same_event"
        clone.pair_fingerprint = f"positive-{index}"
        only_positive.append((clone, report_a, report_b))
    with pytest.raises(ResearchDatasetError, match="both event-pair labels"):
        prepare_dataset_snapshot(only_positive)


def test_prepare_snapshot_ignores_unreviewed_and_privacy_pending_labels():
    rows = _labels()
    for label, _report_a, _report_b in rows[:5]:
        label.review_status = "pending"
    rows[5][0].privacy_review_status = "pending"
    with pytest.raises(ResearchDatasetError, match="at least 12 unique approved pairs"):
        prepare_dataset_snapshot(rows)



def test_prepare_snapshot_rejects_same_report_across_split_groups():
    rows = _labels()
    first_label, report_a, report_b = rows[0]
    other = _labels(1)[0]
    other_label, _other_a, other_b = other
    other_label.split_group = "independent-disaster-group"
    other_label.provenance_snapshot = {
        "report_a": {
            "report_id": report_a.id,
            "content_sha256": report_content_fingerprint(report_a),
        },
        "report_b": {
            "report_id": other_b.id,
            "content_sha256": report_content_fingerprint(other_b),
        },
    }
    other_label.pair_fingerprint = pair_fingerprint(report_a.id, other_b.id)
    with pytest.raises(ResearchDatasetError, match="report appears in multiple split_group"):
        prepare_dataset_snapshot(rows + [(other_label, report_a, other_b)])


def test_prepare_snapshot_rejects_source_content_changed_after_label_creation():
    rows = _labels()
    rows[0][1].description += " A later edit changed the source observation."
    with pytest.raises(ResearchDatasetError, match="content changed after annotation"):
        prepare_dataset_snapshot(rows)





def test_training_vocabulary_text_matches_title_plus_description_features():
    row = {
        "title_a": "Distinct report title token",
        "text_a": "Description body with an independent observation.",
        "title_b": "Another separate title",
        "text_b": "Another report description body.",
    }
    texts = _training_texts([row])
    assert "Distinct report title token" in texts[0]
    assert "Description body with an independent observation." in texts[0]


def test_external_pair_jsonl_rejects_source_report_text_leakage_across_groups(tmp_path):
    path = tmp_path / "leaky.jsonl"
    rows = [
        {
            "split_group": "disaster-alpha",
            "text_a": "Shared source report text with enough characters.",
            "text_b": f"Independent report text for pair alpha {index}.",
            "same_event": index % 2,
        }
        for index in range(2)
    ]
    rows.append({
        "split_group": "disaster-beta",
        "text_a": "Shared source report text with enough characters.",
        "text_b": "An entirely different report from another event.",
        "same_event": 1,
    })
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(EventPairDatasetError, match="report-level leakage"):
        load_event_pair_jsonl(path)

def test_prepare_snapshot_keeps_title_and_description_separate_for_training_features():
    result = prepare_dataset_snapshot(_labels())
    row = result["records"][0]
    assert row["title_a"].startswith("Flood report")
    assert row["text_a"].startswith("Flood water observed")
    assert not row["text_a"].startswith(row["title_a"])


def test_prepare_snapshot_requires_two_mixed_groups_for_a_valid_group_holdout():
    rows = _labels()
    for index, (label, _report_a, _report_b) in enumerate(rows):
        label.label = "same_event" if index < 8 else "different_event"
    with pytest.raises(ResearchDatasetError, match="two split groups must contain both labels"):
        prepare_dataset_snapshot(rows)

def test_dataset_snapshot_rounds_coordinates_to_configured_privacy_precision():
    rows = _labels()
    rows[0][1].latitude = 23.123456
    rows[0][1].longitude = 72.987654
    rows[0][0].provenance_snapshot["report_a"]["content_sha256"] = report_content_fingerprint(rows[0][1])
    result = prepare_dataset_snapshot(rows, coordinate_precision=2)
    first = next(
        row for row in result["records"]
        if row["pair_fingerprint"] == rows[0][0].pair_fingerprint
    )
    assert first["latitude_a"] == 23.12
    assert first["longitude_a"] == 72.99
    assert result["coordinate_precision"] == 2


def test_dataset_snapshot_rejects_non_finite_coordinates():
    rows = _labels()
    rows[0][1].latitude = float("nan")
    rows[0][0].provenance_snapshot["report_a"]["content_sha256"] = report_content_fingerprint(rows[0][1])
    with pytest.raises(ResearchDatasetError, match="invalid geospatial coordinates"):
        prepare_dataset_snapshot(rows)

def test_prepare_snapshot_redacts_identifiers_and_preserves_non_pii_provenance():
    rows = _labels()
    rows[0][1].description += " Email: responder@example.org"
    rows[0][2].description += " Phone: 9876543210"
    rows[0][0].provenance_snapshot["report_a"]["content_sha256"] = report_content_fingerprint(rows[0][1])
    rows[0][0].provenance_snapshot["report_b"]["content_sha256"] = report_content_fingerprint(rows[0][2])
    result = prepare_dataset_snapshot(rows)
    serialized = canonical_jsonl(result["records"]).decode("utf-8")
    assert "responder@example.org" not in serialized
    assert "9876543210" not in serialized
    assert result["redaction_counts"]["email"] == 1
    assert result["redaction_counts"]["phone"] == 1
    assert "report_id" in result["provenance"][0]["report_a"]
    assert "content_sha256" in result["provenance"][0]["label"]["source_snapshot_at_label_creation"]["report_a"]
    assert "submitted_by" not in result["provenance"][0]["report_a"]
