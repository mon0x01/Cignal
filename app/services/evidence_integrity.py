"""Evidence-file integrity checks and auditable batch scans.

The stored SHA-256 is a post-ingest integrity reference. A successful check proves
that the stored bytes still match that reference; it does not prove source authenticity.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.db.models import Evidence
from app.services.audit import record_audit
from app.storage.factory import get_storage

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_BATCH_SIZE = 250
MAX_BATCH_SIZE = 2_000
MAX_FAILURE_SAMPLES = 100


def _is_missing_object_error(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    error_data = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    code = str(error_data.get("Code", "")) if isinstance(error_data, dict) else ""
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


def verify_evidence_record(evidence: Evidence, storage=None) -> dict:
    """Verify bytes against the upload-time digest and size without modifying evidence."""
    storage = storage or get_storage()
    try:
        content = storage.read(evidence.stored_key)
    except Exception as exc:
        status = "missing" if _is_missing_object_error(exc) else "unreadable"
        return {"status": status, "error_type": type(exc).__name__}

    actual_size = len(content)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    expected_sha256 = evidence.sha256 if isinstance(evidence.sha256, str) else ""
    expected_size = evidence.size_bytes

    if not _SHA256_PATTERN.fullmatch(expected_sha256) or not isinstance(expected_size, int) or expected_size < 0:
        return {
            "status": "metadata_invalid",
            "expected_sha256": expected_sha256[:64],
            "actual_sha256": actual_sha256,
            "expected_size": expected_size,
            "actual_size": actual_size,
        }

    hash_matches = hmac.compare_digest(expected_sha256, actual_sha256)
    size_matches = expected_size == actual_size
    if hash_matches and size_matches:
        status = "ok"
    elif not hash_matches and not size_matches:
        status = "hash_and_size_mismatch"
    elif not hash_matches:
        status = "hash_mismatch"
    else:
        status = "size_mismatch"

    return {
        "status": status,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
        "expected_size": expected_size,
        "actual_size": actual_size,
    }


def audit_evidence_integrity(
    db: Session,
    *,
    run_id: str | None = None,
    storage=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Scan every evidence row in bounded DB batches and persist a summary audit record."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")

    storage = storage or get_storage()
    run_id = run_id or str(uuid4())
    started_at = datetime.now(UTC)
    checked = 0
    passed = 0
    failed = 0
    failure_counts: Counter[str] = Counter()
    failure_samples: list[dict[str, str]] = []
    last_created_at = None
    last_id = ""

    while True:
        query = db.query(Evidence).filter(Evidence.created_at <= started_at)
        if last_created_at is not None:
            query = query.filter(
                or_(
                    Evidence.created_at > last_created_at,
                    and_(Evidence.created_at == last_created_at, Evidence.id > last_id),
                )
            )
        rows = (
            query.order_by(Evidence.created_at.asc(), Evidence.id.asc())
            .limit(batch_size)
            .all()
        )
        if not rows:
            break

        for evidence in rows:
            result = verify_evidence_record(evidence, storage)
            checked += 1
            if result["status"] == "ok":
                passed += 1
                continue

            failed += 1
            failure_counts[result["status"]] += 1
            if len(failure_samples) < MAX_FAILURE_SAMPLES:
                failure_samples.append({"evidence_id": evidence.id, "status": result["status"]})
            record_audit(
                db,
                "evidence.integrity_check_failed",
                "evidence",
                evidence.id,
                details={"run_id": run_id, **result},
            )

        last_created_at = rows[-1].created_at
        last_id = rows[-1].id
        # Bound transaction size when a large archive contains many evidence rows.
        db.commit()

    completed_at = datetime.now(UTC)
    summary = {
        "run_id": run_id,
        "status": "completed",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "checked": checked,
        "passed": passed,
        "failed": failed,
        "failure_counts": dict(sorted(failure_counts.items())),
        "failure_samples": failure_samples,
        "failure_samples_truncated": failed > len(failure_samples),
    }
    record_audit(
        db,
        "evidence.integrity_audit_completed",
        "evidence_integrity_audit",
        run_id,
        details=summary,
    )
    db.commit()
    return summary
