"""Governed, reproducible event-pair dataset snapshots."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.db.models import EventPairLabel, Report

SCHEMA_VERSION = "cignal_event_pair_jsonl_v1"
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_INDIA_MOBILE_RE = re.compile(r"(?<!\w)(?:\+91[\s.-]?)?[6-9]\d{9}(?!\w)")
_INTL_PHONE_RE = re.compile(r"(?<!\w)\+\d{1,3}[\s().-]?(?:\d[\s().-]?){7,12}\d(?!\w)")


class ResearchDatasetError(ValueError):
    """Raised when a dataset cannot be safely sealed or exported."""


def pair_fingerprint(report_a_id: str, report_b_id: str) -> str:
    first, second = sorted((str(report_a_id), str(report_b_id)))
    if first == second:
        raise ResearchDatasetError("A report cannot be paired with itself")
    return hashlib.sha256(f"{first}\0{second}".encode()).hexdigest()


def redact_obvious_identifiers(value: str | None) -> tuple[str, dict[str, int]]:
    """Redact obvious email/mobile/intl phone patterns; not a substitute for human review."""
    text = str(value or "")
    counts = {"email": 0, "phone": 0}

    def replace_email(_match):
        counts["email"] += 1
        return "[REDACTED_EMAIL]"

    def replace_phone(_match):
        counts["phone"] += 1
        return "[REDACTED_PHONE]"

    text = _EMAIL_RE.sub(replace_email, text)
    text = _INDIA_MOBILE_RE.sub(replace_phone, text)
    text = _INTL_PHONE_RE.sub(replace_phone, text)
    return text, counts


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _text_for_report(report: Report) -> tuple[str, str, dict[str, int]]:
    title, title_counts = redact_obvious_identifiers(report.title)
    description, description_counts = redact_obvious_identifiers(report.description)
    combined = f"{title}\n{description}".strip()
    if len(combined) < 8:
        raise ResearchDatasetError(f"Report {report.id} does not contain enough usable text")
    return title, description, {
        key: title_counts[key] + description_counts[key]
        for key in ("email", "phone")
    }


def report_source_metadata_fingerprint(report: Report) -> str:
    """Hash the source catalogue/provenance snapshot independently from report text."""
    metadata = getattr(report, "source_metadata", None) or {}
    payload = json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def report_content_fingerprint(report: Report) -> str:
    """Hash all report fields used by training/provenance at label creation time."""
    snapshot = {
        "title": str(report.title or ""),
        "description": str(report.description or ""),
        "disaster_type": str(report.disaster_type or "unknown"),
        "latitude": report.latitude,
        "longitude": report.longitude,
        "source_observed_at": _iso(report.source_observed_at),
        "created_at": _iso(report.created_at),
        "language": str(report.language or "unknown"),
        "source_type": str(report.source_type or "unknown"),
        "source_name": str(report.source_name or "unknown"),
        "source_record_id_present": bool(report.source_record_id),
    }
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _coordinate(value: float | None, *, precision: int, lower: float, upper: float) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not lower <= numeric <= upper:
        raise ResearchDatasetError("Report contains invalid geospatial coordinates.")
    return round(numeric, precision)


def build_pair_row(
    label: EventPairLabel,
    first: Report,
    second: Report,
    *,
    coordinate_precision: int,
) -> tuple[dict, dict, dict[str, int]]:
    """Create a deterministic, privacy-screened trainer row and separate provenance."""
    label_provenance = getattr(label, "provenance_snapshot", None) or {}
    for key, report in (("report_a", first), ("report_b", second)):
        captured = label_provenance.get(key) or {}
        if captured.get("report_id") != report.id:
            raise ResearchDatasetError(
                "Pair label is missing a valid report provenance snapshot; recreate the label."
            )
        if captured.get("content_sha256") != report_content_fingerprint(report):
            raise ResearchDatasetError(
                "Source report content changed after annotation; relabel the current report version."
            )
        source_snapshot_hash = captured.get("source_metadata_sha256")
        if source_snapshot_hash and source_snapshot_hash != report_source_metadata_fingerprint(report):
            raise ResearchDatasetError(
                "Source governance metadata changed after annotation; recreate the label against the current provenance."
            )
    title_a, text_a, redactions_a = _text_for_report(first)
    title_b, text_b, redactions_b = _text_for_report(second)
    row = {
        "pair_fingerprint": label.pair_fingerprint,
        "split_group": label.split_group,
        "text_a": text_a,
        "text_b": text_b,
        "title_a": title_a,
        "title_b": title_b,
        "disaster_type_a": first.disaster_type or "unknown",
        "disaster_type_b": second.disaster_type or "unknown",
        "latitude_a": _coordinate(first.latitude, precision=coordinate_precision, lower=-90, upper=90),
        "longitude_a": _coordinate(first.longitude, precision=coordinate_precision, lower=-180, upper=180),
        "latitude_b": _coordinate(second.latitude, precision=coordinate_precision, lower=-90, upper=90),
        "longitude_b": _coordinate(second.longitude, precision=coordinate_precision, lower=-180, upper=180),
        "observed_at_a": _iso(first.source_observed_at or first.created_at),
        "observed_at_b": _iso(second.source_observed_at or second.created_at),
        "language_a": first.language or "unknown",
        "language_b": second.language or "unknown",
        "source_type_a": first.source_type,
        "source_type_b": second.source_type,
        "same_event": 1 if label.label == "same_event" else 0,
    }
    provenance = {
        "report_a": {
            "report_id": first.id,
            "source_type": first.source_type,
            "source_name": first.source_name,
            "source_record_id_present": bool(first.source_record_id),
            "source_observed_at": _iso(first.source_observed_at),
            "source_metadata_sha256": report_source_metadata_fingerprint(first),
            "source_governance": getattr(first, "source_metadata", None) or {},
            "created_at": _iso(first.created_at),
        },
        "report_b": {
            "report_id": second.id,
            "source_type": second.source_type,
            "source_name": second.source_name,
            "source_record_id_present": bool(second.source_record_id),
            "source_observed_at": _iso(second.source_observed_at),
            "source_metadata_sha256": report_source_metadata_fingerprint(second),
            "source_governance": getattr(second, "source_metadata", None) or {},
            "created_at": _iso(second.created_at),
        },
        "label": {
            "label_method": label.label_method,
            "review_status": label.review_status,
            "privacy_review_status": label.privacy_review_status,
            "reviewed_at": _iso(label.reviewed_at),
            "source_snapshot_at_label_creation": label_provenance,
        },
    }
    redactions = {
        key: redactions_a[key] + redactions_b[key]
        for key in ("email", "phone")
    }
    return row, provenance, redactions


def canonical_jsonl(rows: list[dict]) -> bytes:
    return "".join(
        json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    ).encode()


def prepare_dataset_snapshot(
    label_report_pairs: list[tuple[EventPairLabel, Report, Report]],
    *,
    minimum_records: int = 12,
    minimum_groups: int = 4,
    coordinate_precision: int = 2,
) -> dict[str, Any]:
    """Validate groups/classes/leakage and produce frozen export records + manifest metrics."""
    if minimum_records < 12 or minimum_groups < 4:
        raise ResearchDatasetError("Sealed datasets require at least 12 records and four groups")
    if coordinate_precision < 0 or coordinate_precision > 4:
        raise ResearchDatasetError("coordinate_precision must be between 0 and 4 decimals")
    export_rows: list[dict] = []
    provenance_rows: list[dict] = []
    redactions = Counter()
    seen_content_pairs: dict[str, tuple[str, int]] = {}
    seen_report_groups: dict[str, str] = {}
    seen_report_content_groups: dict[str, str] = {}

    for label, report_a, report_b in label_report_pairs:
        if label.review_status != "approved" or label.privacy_review_status != "passed":
            continue
        if label.label not in {"same_event", "different_event"}:
            raise ResearchDatasetError("Approved pair label has an unsupported target value.")
        if not label.split_group or label.split_group.strip() != label.split_group:
            raise ResearchDatasetError("split_group must be a non-empty, normalized identifier.")
        row, provenance, row_redactions = build_pair_row(
            label, report_a, report_b, coordinate_precision=coordinate_precision
        )
        full_text_a = f"{row['title_a']}\n{row['text_a']}".strip().casefold()
        full_text_b = f"{row['title_b']}\n{row['text_b']}".strip().casefold()
        text_pair = tuple(sorted((full_text_a, full_text_b)))
        text_fingerprint = hashlib.sha256(
            f"{text_pair[0]}\0{text_pair[1]}".encode()
        ).hexdigest()
        group = row["split_group"]
        _, group_redactions = redact_obvious_identifiers(group)
        if any(group_redactions.values()):
            raise ResearchDatasetError("split_group must be a non-personal event/campaign identifier")
        for report, text_value in ((report_a, full_text_a), (report_b, full_text_b)):
            previous_group = seen_report_groups.setdefault(str(report.id), group)
            if previous_group != group:
                raise ResearchDatasetError(
                    "A source report appears in multiple split_group values; prevent report-level leakage."
                )
            content_hash = hashlib.sha256(text_value.casefold().encode()).hexdigest()
            previous_content_group = seen_report_content_groups.setdefault(content_hash, group)
            if previous_content_group != group:
                raise ResearchDatasetError(
                    "Identical source report content appears in multiple split_group values; prevent content leakage."
                )
        target = row["same_event"]
        previous = seen_content_pairs.get(text_fingerprint)
        if previous and previous[0] != group:
            raise ResearchDatasetError(
                "Identical text pair appears in multiple split_group values; resolve group leakage."
            )
        if previous and previous[1] != target:
            raise ResearchDatasetError(
                "Identical text pair has conflicting labels; resolve annotation conflict."
            )
        if previous:
            continue
        seen_content_pairs[text_fingerprint] = (group, target)
        export_rows.append(row)
        provenance_rows.append(provenance)
        redactions.update(row_redactions)

    same_count = sum(row["same_event"] == 1 for row in export_rows)
    different_count = len(export_rows) - same_count
    groups = {row["split_group"] for row in export_rows}
    positive_groups = {
        row["split_group"] for row in export_rows if row["same_event"] == 1
    }
    negative_groups = {
        row["split_group"] for row in export_rows if row["same_event"] == 0
    }
    if len(export_rows) < minimum_records:
        raise ResearchDatasetError(
            f"Need at least {minimum_records} unique approved pairs; found {len(export_rows)}."
        )
    if len(groups) < minimum_groups:
        raise ResearchDatasetError(
            f"Need at least {minimum_groups} independent split groups; found {len(groups)}."
        )
    if same_count == 0 or different_count == 0:
        raise ResearchDatasetError("A sealed dataset must contain both event-pair labels.")
    if len(positive_groups) < 2 or len(negative_groups) < 2:
        raise ResearchDatasetError(
            "Both labels must occur in at least two independent split groups "
            "to support a group-disjoint holdout."
        )
    mixed_groups = positive_groups & negative_groups
    if len(mixed_groups) < 2:
        raise ResearchDatasetError(
            "At least two split groups must contain both labels so a group-disjoint "
            "holdout can retain both classes in train and evaluation."
        )

    ordered = sorted(
        zip(export_rows, provenance_rows, strict=True),
        key=lambda item: (item[0]["split_group"], item[0]["pair_fingerprint"]),
    )
    export_rows = [item[0] for item in ordered]
    provenance_rows = [item[1] for item in ordered]
    payload = canonical_jsonl(export_rows)
    source_types = Counter(
        report_source
        for provenance in provenance_rows
        for report_source in (
            provenance["report_a"]["source_type"],
            provenance["report_b"]["source_type"],
        )
    )
    label_methods = Counter(provenance["label"]["label_method"] for provenance in provenance_rows)
    source_license_statuses = Counter(
        (report_provenance.get("source_governance") or {}).get("license_status", "missing_source_governance")
        for provenance in provenance_rows
        for report_provenance in (provenance["report_a"], provenance["report_b"])
    )
    source_catalog_versions = sorted({
        str((report_provenance.get("source_governance") or {}).get("catalog_version", "unknown"))
        for provenance in provenance_rows
        for report_provenance in (provenance["report_a"], provenance["report_b"])
    })
    return {
        "records": export_rows,
        "provenance": provenance_rows,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "record_count": len(export_rows),
        "same_event_count": same_count,
        "different_event_count": different_count,
        "split_group_count": len(groups),
        "positive_group_count": len(positive_groups),
        "negative_group_count": len(negative_groups),
        "mixed_group_count": len(mixed_groups),
        "redaction_counts": dict(redactions),
        "source_type_counts": dict(source_types),
        "source_license_status_counts": dict(source_license_statuses),
        "source_catalog_versions": source_catalog_versions,
        "label_method_counts": dict(label_methods),
        "split_groups": sorted(groups),
        "schema_version": SCHEMA_VERSION,
        "coordinate_precision": coordinate_precision,
    }


def serialize_dataset_items(items) -> bytes:
    rows = [item.record_json for item in items]
    return canonical_jsonl(rows)
