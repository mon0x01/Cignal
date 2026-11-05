from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    BackgroundJobExecutionAttempt,
    BackgroundJobOutbox,
    utcnow,
)
from app.services.job_execution import (
    claim_execution,
    complete_execution,
    fail_execution,
    reconcile_enqueued_jobs,
    recover_expired_executions,
)
from app.services.job_outbox import (
    enqueue_event_discovery,
    enqueue_event_replay,
    enqueue_report_analysis,
)


@pytest.fixture
def execution_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'execution.sqlite3').as_posix()}")
    BackgroundJobOutbox.__table__.create(engine)
    BackgroundJobExecutionAttempt.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _create_job(factory, *, timeout=60, status="pending"):
    with factory() as db:
        row = enqueue_report_analysis(db, str(uuid4()), 1)
        row.job_timeout_seconds = timeout
        row.status = status
        row.enqueued_at = utcnow() if status == "enqueued" else None
        row.last_rq_job_id = f"rq-{uuid4()}"
        db.commit()
        return row.id, row.job_id, row.last_rq_job_id


def test_claim_execution_creates_ledger_and_completion_is_idempotent(execution_sessions):
    outbox_id, job_id, rq_id = _create_job(execution_sessions)
    claim = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-a",
        session_factory=execution_sessions,
    )
    assert claim["status"] == "claimed"
    assert claim["job_id"] == job_id
    assert claim["attempt_number"] == 1

    completed = complete_execution(
        outbox_id, claim["attempt_id"], claim["lease_token"],
        result_sha256="a" * 64, session_factory=execution_sessions,
    )
    assert completed is True
    duplicate = claim_execution(
        outbox_id, rq_job_id="rq-duplicate", worker_name="worker-b",
        session_factory=execution_sessions,
    )
    assert duplicate["status"] == "already_completed"

    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        attempt = db.get(BackgroundJobExecutionAttempt, claim["attempt_id"])
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.execution_lease_token is None
        assert attempt.status == "completed"
        assert attempt.result_sha256 == "a" * 64


def test_stale_rq_delivery_cannot_claim_a_new_attempt(execution_sessions):
    outbox_id, _, current_rq_id = _create_job(execution_sessions)
    stale = claim_execution(
        outbox_id, rq_job_id="rq-from-an-older-dispatch", worker_name="stale-worker",
        session_factory=execution_sessions,
    )
    assert stale["status"] == "stale_delivery"
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.execution_attempts == 0
        assert row.status == "pending"
    current = claim_execution(
        outbox_id, rq_job_id=current_rq_id, worker_name="current-worker",
        session_factory=execution_sessions,
    )
    assert current["status"] == "claimed"


def test_live_execution_lease_suppresses_duplicate_workers(execution_sessions):
    outbox_id, _, rq_id = _create_job(execution_sessions)
    first = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-a",
        session_factory=execution_sessions, lease_seconds=600,
    )
    second = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-b",
        session_factory=execution_sessions, lease_seconds=600,
    )
    assert first["status"] == "claimed"
    assert second["status"] == "already_running"
    with execution_sessions() as db:
        assert db.query(BackgroundJobExecutionAttempt).filter_by(outbox_id=outbox_id).count() == 1


def test_failure_uses_execution_retry_budget_and_dead_letters(execution_sessions):
    outbox_id, _, rq_id = _create_job(execution_sessions)
    first = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-a",
        session_factory=execution_sessions, max_attempts=2,
    )
    outcome = fail_execution(
        outbox_id, first["attempt_id"], first["lease_token"], RuntimeError("synthetic"),
        session_factory=execution_sessions, max_attempts=2, now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert outcome == "retried"

    stale_retry = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="manually-requeued-old-delivery",
        session_factory=execution_sessions,
    )
    assert stale_retry["status"] == "stale_delivery"

    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "pending"
        assert row.last_execution_error_code == "RuntimeError"
        assert row.last_rq_job_id is None
        due_at = row.available_at.replace(tzinfo=UTC) if row.available_at.tzinfo is None else row.available_at
        assert due_at > datetime(2026, 1, 1, tzinfo=UTC)
        row.available_at = datetime(2026, 1, 1, tzinfo=UTC)
        row.last_rq_job_id = "rq-2"
        db.commit()

    second = claim_execution(
        outbox_id, rq_job_id="rq-2", worker_name="worker-a",
        session_factory=execution_sessions,
        max_attempts=2,
        now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )
    assert second["status"] == "claimed"
    outcome = fail_execution(
        outbox_id, second["attempt_id"], second["lease_token"], "second_failure",
        session_factory=execution_sessions, max_attempts=2,
        now=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
    )
    assert outcome == "dead"
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "dead"
        assert row.execution_attempts == 2
        assert db.query(BackgroundJobExecutionAttempt).filter_by(outbox_id=outbox_id).count() == 2


def test_expired_execution_lease_is_abandoned_and_requeued(execution_sessions):
    outbox_id, _, rq_id = _create_job(execution_sessions)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    claim = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-lost",
        session_factory=execution_sessions, lease_seconds=30, now=start,
    )
    recovered = recover_expired_executions(
        session_factory=execution_sessions, max_attempts=3, now=start + timedelta(seconds=31),
    )
    assert recovered == {"checked": 1, "requeued": 1, "dead": 0}
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        attempt = db.get(BackgroundJobExecutionAttempt, claim["attempt_id"])
        assert row.status == "pending"
        assert row.execution_lease_token is None
        assert row.last_execution_error_code == "execution_lease_expired"
        assert row.last_rq_job_id is None
        assert attempt.status == "abandoned"
        assert attempt.error_code == "execution_lease_expired"


def test_expired_execution_exhaustion_moves_job_to_dead_letter(execution_sessions):
    outbox_id, _, rq_id = _create_job(execution_sessions)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    claim = claim_execution(
        outbox_id, rq_job_id=rq_id, worker_name="worker-lost",
        session_factory=execution_sessions, lease_seconds=30, max_attempts=1, now=start,
    )
    recovered = recover_expired_executions(
        session_factory=execution_sessions, max_attempts=1, now=start + timedelta(seconds=31),
    )
    assert recovered == {"checked": 1, "requeued": 0, "dead": 1}
    with execution_sessions() as db:
        assert db.get(BackgroundJobOutbox, outbox_id).status == "dead"
        assert db.get(BackgroundJobExecutionAttempt, claim["attempt_id"]).status == "abandoned"


class _QueueJob:
    def __init__(self, status):
        self.status = status

    def get_status(self, refresh=True):
        return self.status


class _Queue:
    def __init__(self, status=None):
        self.job = _QueueJob(status) if status else None

    def fetch_job(self, job_id):
        return self.job


class _RecordingQueue:
    def __init__(self):
        self.jobs = {}

    def fetch_job(self, job_id):
        return self.jobs.get(job_id)

    def enqueue(self, function_path, *args, **kwargs):
        job = _QueueJob("queued")
        job.id = kwargs["job_id"]
        job.function_path = function_path
        job.args = args
        self.jobs[job.id] = job
        return job


def test_stale_failed_rq_delivery_is_requeued(execution_sessions):
    outbox_id, _, rq_id = _create_job(execution_sessions, timeout=60, status="enqueued")
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        row.enqueued_at = old
        db.commit()

    result = reconcile_enqueued_jobs(
        {"cignal-priority": _Queue("failed")},
        session_factory=execution_sessions,
        grace_seconds=30,
        lease_grace_seconds=30,
        now=old + timedelta(seconds=200),
    )
    assert result["requeued"] == 1
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "pending"
        assert row.last_error_code == "rq_failed"
        assert row.last_rq_job_id is None  # the failed delivery is retired


def test_finished_legacy_rq_delivery_is_marked_completed(execution_sessions):
    outbox_id, _, _ = _create_job(execution_sessions, timeout=60, status="enqueued")
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        row.enqueued_at = old
        db.commit()
    result = reconcile_enqueued_jobs(
        {"cignal-priority": _Queue("finished")},
        session_factory=execution_sessions,
        grace_seconds=30,
        lease_grace_seconds=30,
        now=old + timedelta(seconds=200),
    )
    assert result["completed"] == 1
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "completed"
        assert row.completed_at is not None


def test_queued_rq_delivery_is_not_recovered_prematurely(execution_sessions):
    outbox_id, _, _ = _create_job(execution_sessions, timeout=60, status="enqueued")
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_sessions() as db:
        db.get(BackgroundJobOutbox, outbox_id).enqueued_at = old
        db.commit()
    result = reconcile_enqueued_jobs(
        {"cignal-priority": _Queue("queued")},
        session_factory=execution_sessions,
        grace_seconds=30,
        lease_grace_seconds=30,
        now=old + timedelta(seconds=200),
    )
    assert result["waiting"] == 1
    with execution_sessions() as db:
        assert db.get(BackgroundJobOutbox, outbox_id).status == "enqueued"


def test_dispatch_attempts_route_to_priority_normal_and_bulk_queues(execution_sessions):
    queues = {
        "cignal-priority": _RecordingQueue(),
        "cignal": _RecordingQueue(),
        "cignal-bulk": _RecordingQueue(),
    }
    with execution_sessions() as db:
        report = enqueue_report_analysis(db, "priority-report", 1)
        discovery = enqueue_event_discovery(db, "discovery-abc", {"window_hours": 72})
        replay = enqueue_event_replay(db, "replay-abc")
        ids = {report.id, discovery.id, replay.id}
        db.commit()

    from app.services.job_outbox import dispatch_pending_jobs

    dispatched = dispatch_pending_jobs(queues, session_factory=execution_sessions)
    assert dispatched["enqueued"] == 3
    with execution_sessions() as db:
        rows = {row.id: row for row in db.query(BackgroundJobOutbox).all()}
        assert rows[report.id].queue_name == "cignal-priority"
        assert rows[discovery.id].queue_name == "cignal"
        assert rows[replay.id].queue_name == "cignal-bulk"
        assert all(rows[item].status == "enqueued" for item in ids)
    assert len(queues["cignal-priority"].jobs) == 1
    assert len(queues["cignal"].jobs) == 1
    assert len(queues["cignal-bulk"].jobs) == 1


def test_fast_worker_completion_before_relay_ack_keeps_completed_state(execution_sessions):
    from app.services.job_execution import claim_execution, complete_execution
    from app.services.job_outbox import dispatch_pending_jobs

    class CompletingQueue(_RecordingQueue):
        def enqueue(self, function_path, *args, **kwargs):
            job = super().enqueue(function_path, *args, **kwargs)
            outbox_id, rq_job_id = args
            claim = claim_execution(
                outbox_id,
                rq_job_id=rq_job_id,
                worker_name="fast-worker",
                session_factory=execution_sessions,
            )
            assert claim["status"] == "claimed"
            assert complete_execution(
                outbox_id,
                claim["attempt_id"],
                claim["lease_token"],
                result_sha256="b" * 64,
                session_factory=execution_sessions,
            )
            return job

    with execution_sessions() as db:
        row = enqueue_report_analysis(db, str(uuid4()), 1)
        outbox_id = row.id
        logical_job_id = row.job_id
        db.commit()

    result = dispatch_pending_jobs(CompletingQueue(), session_factory=execution_sessions)

    assert result["enqueued"] == 1
    assert result["lease_lost"] == 0
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "completed"
        assert row.completed_at is not None
        assert row.last_rq_job_id
        assert row.lease_token is None
        assert row.job_id == logical_job_id


def test_expired_legacy_publishing_lease_adopts_existing_rq_delivery(execution_sessions):
    from app.services.job_outbox import dispatch_pending_jobs

    outbox_id, _, _ = _create_job(execution_sessions)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        row.status = "publishing"
        row.attempts = 1
        row.last_rq_job_id = "legacy-rq-delivery"
        row.lease_token = "expired-publisher"
        row.lease_expires_at = old - timedelta(seconds=5)
        row.updated_at = old - timedelta(seconds=10)
        db.commit()

    queue = _Queue("queued")
    queues = {"cignal-priority": queue}
    dispatched = dispatch_pending_jobs(queues, session_factory=execution_sessions)
    assert dispatched["claimed"] == 0

    recovered = reconcile_enqueued_jobs(
        queues,
        session_factory=execution_sessions,
        now=old,
    )
    assert recovered["waiting"] == 1
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "enqueued"
        assert row.last_rq_job_id == "legacy-rq-delivery"
        assert row.enqueued_at is not None
        assert row.lease_token is None
        assert row.lease_expires_at is None


def test_expired_legacy_publishing_lease_with_missing_rq_job_is_requeued(execution_sessions):
    outbox_id, _, _ = _create_job(execution_sessions)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        row.status = "publishing"
        row.attempts = 1
        row.last_rq_job_id = "legacy-rq-missing"
        row.lease_token = "expired-publisher"
        row.lease_expires_at = old - timedelta(seconds=5)
        row.updated_at = old - timedelta(seconds=10)
        db.commit()

    recovered = reconcile_enqueued_jobs(
        {"cignal-priority": _Queue()},
        session_factory=execution_sessions,
        now=old,
    )
    assert recovered["requeued"] == 1
    with execution_sessions() as db:
        row = db.get(BackgroundJobOutbox, outbox_id)
        assert row.status == "pending"
        assert row.last_error_code == "rq_missing"
        assert row.lease_token is None
        assert row.lease_expires_at is None
