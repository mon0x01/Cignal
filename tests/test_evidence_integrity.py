from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AuditLog, Base, Evidence, Report
from app.services.evidence_integrity import audit_evidence_integrity, verify_evidence_record


class MemoryStorage:
    def __init__(self, objects=None, errors=None):
        self.objects = objects or {}
        self.errors = errors or {}

    def read(self, key):
        if key in self.errors:
            raise self.errors[key]
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def _evidence(key: str, payload: bytes, *, stored_size: int | None = None, stored_hash: str | None = None):
    return SimpleNamespace(
        id=str(uuid4()),
        stored_key=key,
        sha256=stored_hash if stored_hash is not None else hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload) if stored_size is None else stored_size,
    )


def test_integrity_verification_accepts_unchanged_evidence():
    payload = b"original evidence bytes"
    evidence = _evidence("report/evidence.bin", payload)
    result = verify_evidence_record(evidence, MemoryStorage({evidence.stored_key: payload}))
    assert result["status"] == "ok"
    assert result["actual_sha256"] == evidence.sha256
    assert result["actual_size"] == len(payload)


def test_integrity_verification_detects_hash_and_size_tampering():
    evidence = _evidence("report/evidence.bin", b"original")
    result = verify_evidence_record(evidence, MemoryStorage({evidence.stored_key: b"modified bytes"}))
    assert result["status"] == "hash_and_size_mismatch"
    assert result["actual_sha256"] != result["expected_sha256"]


def test_integrity_verification_distinguishes_missing_and_unreadable_objects():
    missing = _evidence("missing.bin", b"expected")
    unreadable = _evidence("locked.bin", b"expected")
    storage = MemoryStorage(errors={"locked.bin": PermissionError("denied")})
    assert verify_evidence_record(missing, storage)["status"] == "missing"
    unreadable_result = verify_evidence_record(unreadable, storage)
    assert unreadable_result["status"] == "unreadable"
    assert unreadable_result["error_type"] == "PermissionError"


def test_integrity_verification_detects_size_only_mismatch():
    payload = b"unchanged payload"
    evidence = _evidence("evidence.bin", payload, stored_size=len(payload) + 1)
    result = verify_evidence_record(evidence, MemoryStorage({"evidence.bin": payload}))
    assert result["status"] == "size_mismatch"
    assert result["actual_sha256"] == result["expected_sha256"]


def test_integrity_verification_rejects_invalid_hash_metadata():
    evidence = _evidence("evidence.bin", b"bytes", stored_hash="not-a-sha256")
    result = verify_evidence_record(evidence, MemoryStorage({"evidence.bin": b"bytes"}))
    assert result["status"] == "metadata_invalid"


def test_integrity_verification_recognizes_s3_missing_object_response():
    class MissingS3Object(Exception):
        response = {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    evidence = _evidence("s3/missing.bin", b"expected")
    result = verify_evidence_record(
        evidence, MemoryStorage(errors={"s3/missing.bin": MissingS3Object()})
    )
    assert result["status"] == "missing"


def test_batch_audit_persists_failures_and_summary_without_mutating_evidence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    good_bytes = b"good"
    corrupt_expected = b"before-tampering"
    with session_factory() as db:
        report = Report(
            id="report-1", tracking_code="CIG-R-00000001", title="Test report",
            description="Synthetic test record", disaster_type="other", urgency="low",
            status="submitted", language="en", source_type="citizen", source_name="Citizen",
            provenance="submitted",
        )
        db.add(report)
        db.flush()
        rows = [
            Evidence(
                id="evidence-a", report_id=report.id, original_filename="good.bin",
                stored_key="good.bin", mime_type="application/octet-stream",
                sha256=hashlib.sha256(good_bytes).hexdigest(), size_bytes=len(good_bytes),
            ),
            Evidence(
                id="evidence-b", report_id=report.id, original_filename="corrupt.bin",
                stored_key="corrupt.bin", mime_type="application/octet-stream",
                sha256=hashlib.sha256(corrupt_expected).hexdigest(), size_bytes=len(corrupt_expected),
            ),
            Evidence(
                id="evidence-c", report_id=report.id, original_filename="missing.bin",
                stored_key="missing.bin", mime_type="application/octet-stream",
                sha256=hashlib.sha256(b"missing").hexdigest(), size_bytes=7,
            ),
        ]
        db.add_all(rows)
        db.commit()

        summary = audit_evidence_integrity(
            db,
            run_id="12345678-1234-5678-1234-567812345678",
            storage=MemoryStorage({"good.bin": good_bytes, "corrupt.bin": b"modified"}),
            batch_size=1,
        )

        assert summary["checked"] == 3
        assert summary["passed"] == 1
        assert summary["failed"] == 2
        assert summary["failure_counts"] == {"hash_and_size_mismatch": 1, "missing": 1}
        assert db.query(AuditLog).filter_by(action="evidence.integrity_check_failed").count() == 2
        completed = db.query(AuditLog).filter_by(action="evidence.integrity_audit_completed").one()
        assert completed.resource_id == summary["run_id"]
        assert completed.details["checked"] == 3
        assert db.get(Evidence, "evidence-b").sha256 == hashlib.sha256(corrupt_expected).hexdigest()
    engine.dispose()


def test_integrity_audit_rejects_invalid_batch_size():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        with pytest.raises(ValueError):
            audit_evidence_integrity(db, storage=MemoryStorage(), batch_size=0)
    engine.dispose()


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.released = False

    def acquire(self, blocking=False):
        assert blocking is False
        return self.acquired

    def release(self):
        self.released = True


class FakeRedis:
    def __init__(self, lock):
        self.lock_instance = lock
        self.lock_args = None

    def lock(self, *args, **kwargs):
        self.lock_args = (args, kwargs)
        return self.lock_instance


class FakeQueue:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def enqueue(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error


def test_daily_scheduler_enqueues_only_when_lock_is_acquired():
    from app.services.scheduler_jobs import enqueue_daily_evidence_integrity_audit

    lock = FakeLock(acquired=True)
    redis = FakeRedis(lock)
    queue = FakeQueue()
    run_id = enqueue_daily_evidence_integrity_audit(redis, queue)
    assert run_id is not None
    assert queue.calls[0][0] == ("app.worker.run_evidence_integrity_audit", run_id)
    assert queue.calls[0][1] == {"job_timeout": 21600, "result_ttl": 86400}
    assert redis.lock_args[0] == ("cignal:scheduler:evidence-integrity",)
    assert lock.released is False  # Lease intentionally prevents duplicate daily runs.


def test_daily_scheduler_skips_when_disabled_or_already_scheduled():
    from app.services.scheduler_jobs import enqueue_daily_evidence_integrity_audit

    disabled_queue = FakeQueue()
    assert enqueue_daily_evidence_integrity_audit(FakeRedis(FakeLock()), disabled_queue, enabled=False) is None
    assert disabled_queue.calls == []

    duplicate_queue = FakeQueue()
    assert enqueue_daily_evidence_integrity_audit(
        FakeRedis(FakeLock(acquired=False)), duplicate_queue
    ) is None
    assert duplicate_queue.calls == []


def test_daily_scheduler_releases_lock_when_enqueue_fails():
    from app.services.scheduler_jobs import enqueue_daily_evidence_integrity_audit

    lock = FakeLock(acquired=True)
    with pytest.raises(RuntimeError):
        enqueue_daily_evidence_integrity_audit(
            FakeRedis(lock), FakeQueue(error=RuntimeError("queue unavailable"))
        )
    assert lock.released is True
