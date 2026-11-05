from __future__ import annotations

from datetime import UTC, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import BackgroundJobOutbox, utcnow
from app.services.job_outbox import (
    dispatch_pending_jobs,
    enqueue_event_discovery,
    enqueue_report_analysis,
    summarize_outbox,
)


class FakeJob:
    def __init__(self, job_id: str, status: str = "queued"):
        self.id = job_id
        self.status = status

    def get_status(self, refresh: bool = False):
        return self.status

    def delete(self):
        self.status = "deleted"


class FakeQueue:
    def __init__(self, *, fail: bool = False):
        self.jobs = {}
        self.fail = fail

    def fetch_job(self, job_id: str):
        return self.jobs.get(job_id)

    def enqueue(self, function_path: str, *args, **kwargs):
        if self.fail:
            raise ConnectionError("synthetic Redis outage")
        job = FakeJob(kwargs["job_id"])
        self.jobs[job.id] = job
        job.function_path = function_path
        job.args = args
        return job


@pytest.fixture
def outbox_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'outbox.sqlite3').as_posix()}")
    BackgroundJobOutbox.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def test_report_analysis_outbox_is_idempotent_and_revision_scoped(outbox_sessions):
    with outbox_sessions() as db:
        first = enqueue_report_analysis(db, "report-123", 1)
        replay = enqueue_report_analysis(db, "report-123", 1)
        second_revision = enqueue_report_analysis(db, "report-123", 2)
        db.commit()

        assert first.id == replay.id
        assert first.job_id == "cignal-report-analysis-report-123-r1"
        assert first.task_args == ["report-123", 1]
        assert second_revision.id != first.id
        assert db.query(BackgroundJobOutbox).count() == 2


def test_dispatch_marks_job_enqueued_and_is_safe_after_scheduler_replay(outbox_sessions):
    with outbox_sessions() as db:
        row = enqueue_report_analysis(db, "report-456", 3)
        row_id = row.id
        db.commit()

    queue = FakeQueue()
    first = dispatch_pending_jobs(queue, session_factory=outbox_sessions)
    second = dispatch_pending_jobs(queue, session_factory=outbox_sessions)

    assert first == {"claimed": 1, "enqueued": 1, "retried": 0, "dead": 0, "lease_lost": 0}
    assert second["claimed"] == 0
    assert len(queue.jobs) == 1
    queued = next(iter(queue.jobs.values()))
    assert queued.function_path == "app.worker.execute_outbox_job"
    assert queued.args[0] == row_id
    assert queued.args[1].startswith("cignal-delivery-")
    with outbox_sessions() as db:
        stored = db.query(BackgroundJobOutbox).one()
        assert stored.status == "enqueued"
        assert stored.enqueued_at is not None
        assert stored.lease_token is None
        assert stored.last_rq_job_id == queued.id


def test_dispatch_failure_uses_exponential_backoff_without_losing_request(outbox_sessions):
    with outbox_sessions() as db:
        enqueue_report_analysis(db, "report-789", 1)
        db.commit()

    result = dispatch_pending_jobs(FakeQueue(fail=True), session_factory=outbox_sessions)
    assert result["claimed"] == 1
    assert result["retried"] == 1

    with outbox_sessions() as db:
        row = db.query(BackgroundJobOutbox).one()
        assert row.status == "pending"
        assert row.attempts == 1
        scheduled = row.available_at if row.available_at.tzinfo else row.available_at.replace(tzinfo=UTC)
        assert scheduled > utcnow()
        assert row.last_error_code == "ConnectionError"
        assert row.lease_token is None


def test_dispatch_dead_letters_after_attempt_budget_and_can_be_summarized(outbox_sessions):
    with outbox_sessions() as db:
        row = enqueue_report_analysis(db, "report-dead", 4)
        row.attempts = 11
        db.commit()

    result = dispatch_pending_jobs(
        FakeQueue(fail=True), session_factory=outbox_sessions, max_attempts=12
    )
    assert result["dead"] == 1
    with outbox_sessions() as db:
        summary = summarize_outbox(db)
        assert summary["counts"]["dead"] == 1
        assert summary["recent"][0]["job_id"] == "cignal-report-analysis-report-dead-r4"


def test_expired_dispatch_lease_is_reclaimed(outbox_sessions):
    with outbox_sessions() as db:
        row = enqueue_report_analysis(db, "report-lease", 5)
        row.status = "publishing"
        row.lease_token = str(uuid4())
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    result = dispatch_pending_jobs(FakeQueue(), session_factory=outbox_sessions)
    assert result["claimed"] == 1
    assert result["enqueued"] == 1
    with outbox_sessions() as db:
        row = db.query(BackgroundJobOutbox).one()
        assert row.status == "enqueued"
        assert row.attempts == 1


def test_event_discovery_outbox_is_idempotent_and_uses_durable_dispatch(outbox_sessions):
    parameters = {"window_hours": 72, "max_reports": 300, "trigger_kind": "continuous"}
    with outbox_sessions() as db:
        first = enqueue_event_discovery(db, "discovery-run-001", parameters)
        replay = enqueue_event_discovery(db, "discovery-run-001", parameters)
        db.commit()
        assert first.id == replay.id
        assert first.job_id == "cignal-event-discovery-discovery-run-001"
        assert first.function_path == "app.worker.discover_event_hypotheses"
        assert first.task_args == ["discovery-run-001", parameters]

    queue = FakeQueue()
    result = dispatch_pending_jobs(queue, session_factory=outbox_sessions)
    assert result["enqueued"] == 1
    queued = next(iter(queue.jobs.values()))
    assert queued.function_path == "app.worker.execute_outbox_job"
    assert len(queued.args) == 2
    assert queued.args[1] == queued.id
    with outbox_sessions() as db:
        stored = db.query(BackgroundJobOutbox).one()
        assert stored.status == "enqueued"
        assert stored.queue_name == "cignal"
        assert stored.last_rq_job_id == queued.id
