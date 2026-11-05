from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.db.models import AcquisitionSourceState
from app.db.session import SessionLocal, initialize_database
from app.services.acquisition_schedule import (
    claim_due_sources,
    complete_source_run,
    configured_source_intervals,
    ensure_source_state,
    lease_is_current,
    release_source_claim,
    source_poll_interval_seconds,
    source_schedule_dict,
)


class ConfiguredConnector:
    def __init__(self, source_type: str, configured: bool = True):
        self.source_type = source_type
        self.name = source_type
        self._configured = configured

    def configured(self):
        return self._configured



def _reset(source_type: str):
    initialize_database()
    with SessionLocal() as db:
        db.query(AcquisitionSourceState).filter_by(source_type=source_type).delete()
        db.commit()


def test_source_schedule_initializes_after_one_interval_not_immediately(monkeypatch):
    source_type = "qa_schedule_initial"
    _reset(source_type)
    monkeypatch.setattr(settings, "ingest_interval_minutes", 30)
    monkeypatch.setattr(settings, "ingest_source_interval_overrides", "")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        assert state.poll_interval_seconds == 1800
        assert state.next_run_at.replace(tzinfo=UTC) == now + timedelta(minutes=30)
        claims = claim_due_sources(db, [ConfiguredConnector(source_type)], now=now)
        assert claims == []


def test_claim_is_single_owner_and_expired_lease_can_be_recovered(monkeypatch):
    source_type = "qa_schedule_lease"
    _reset(source_type)
    monkeypatch.setattr(settings, "ingest_interval_minutes", 30)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()
        connector = ConfiguredConnector(source_type)
        first = claim_due_sources(db, [connector], now=now, lease_seconds=300)
        assert len(first) == 1
        first_token = first[0]["lease_token"]
        assert lease_is_current(db, source_type, first_token, now=now)

        assert claim_due_sources(
            db, [connector], now=now + timedelta(seconds=10), lease_seconds=300
        ) == []

        recovered = claim_due_sources(
            db, [connector], now=now + timedelta(seconds=1261), lease_seconds=300
        )
        assert len(recovered) == 1
        assert recovered[0]["lease_token"] != first_token
        assert lease_is_current(db, source_type, recovered[0]["lease_token"], now=now + timedelta(seconds=1261))
        assert not lease_is_current(db, source_type, first_token, now=now + timedelta(seconds=1261))


def test_retryable_failures_use_capped_exponential_backoff_and_success_resets(monkeypatch):
    source_type = "qa_schedule_backoff"
    _reset(source_type)
    monkeypatch.setattr(settings, "ingest_interval_minutes", 30)
    monkeypatch.setattr(settings, "ingest_retry_backoff_base_seconds", 60)
    monkeypatch.setattr(settings, "ingest_retry_backoff_max_seconds", 180)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()
        first = claim_due_sources(db, [ConfiguredConnector(source_type)], now=now, lease_seconds=300)[0]
        assert complete_source_run(
            db, source_type=source_type, status="failed", run_id="run-1",
            failure_code="TimeoutError", retryable=True,
            lease_token=first["lease_token"], now=now,
        )
        state = db.get(AcquisitionSourceState, source_type)
        assert state.consecutive_failures == 1
        assert state.next_run_at.replace(tzinfo=UTC) == now + timedelta(seconds=60)

        second_at = now + timedelta(seconds=61)
        second = claim_due_sources(db, [ConfiguredConnector(source_type)], now=second_at, lease_seconds=300)[0]
        complete_source_run(
            db, source_type=source_type, status="failed", run_id="run-2",
            failure_code="HTTPStatusError", retryable=True,
            lease_token=second["lease_token"], now=second_at,
        )
        state = db.get(AcquisitionSourceState, source_type)
        assert state.consecutive_failures == 2
        assert state.next_run_at.replace(tzinfo=UTC) == second_at + timedelta(seconds=120)

        third_at = second_at + timedelta(seconds=121)
        third = claim_due_sources(db, [ConfiguredConnector(source_type)], now=third_at, lease_seconds=300)[0]
        complete_source_run(
            db, source_type=source_type, status="failed", run_id="run-3",
            failure_code="HTTPStatusError", retryable=True,
            lease_token=third["lease_token"], now=third_at,
        )
        state = db.get(AcquisitionSourceState, source_type)
        assert state.consecutive_failures == 3
        assert state.next_run_at.replace(tzinfo=UTC) == third_at + timedelta(seconds=180)

        fourth_at = third_at + timedelta(seconds=181)
        fourth = claim_due_sources(db, [ConfiguredConnector(source_type)], now=fourth_at, lease_seconds=300)[0]
        complete_source_run(
            db, source_type=source_type, status="succeeded", run_id="run-4",
            records_fetched=12, records_inserted=10, records_duplicate=2,
            lease_token=fourth["lease_token"], now=fourth_at,
        )
        state = db.get(AcquisitionSourceState, source_type)
        assert state.consecutive_failures == 0
        assert state.last_records_fetched == 12
        assert state.last_records_inserted == 10
        assert state.last_records_duplicate == 2
        assert state.lease_token is None
        assert state.next_run_at.replace(tzinfo=UTC) == fourth_at + timedelta(minutes=30)


def test_old_worker_cannot_complete_after_lease_was_reclaimed(monkeypatch):
    source_type = "qa_schedule_stale_worker"
    _reset(source_type)
    monkeypatch.setattr(settings, "ingest_interval_minutes", 30)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()
        first = claim_due_sources(db, [ConfiguredConnector(source_type)], now=now, lease_seconds=300)[0]
        later = now + timedelta(seconds=1261)
        second = claim_due_sources(db, [ConfiguredConnector(source_type)], now=later, lease_seconds=300)[0]
        assert complete_source_run(
            db, source_type=source_type, status="succeeded", run_id="stale",
            lease_token=first["lease_token"], now=later,
        ) is False
        state = db.get(AcquisitionSourceState, source_type)
        assert state.lease_token == second["lease_token"]
        assert state.last_run_id is None


def test_claim_skips_unconfigured_sources_and_schedule_payload_is_bounded(monkeypatch):
    source_type = "qa_schedule_view"
    _reset(source_type)
    monkeypatch.setattr(settings, "ingest_source_interval_overrides", f"{source_type}=600;bad=abc;too_long=999999")
    assert source_poll_interval_seconds(source_type) == 600
    assert configured_source_intervals() == {source_type: 600}

    now = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        view = source_schedule_dict(state, now=now)
        assert view["seconds_until_next_run"] == 600
        assert view["lease_active"] is False
        assert claim_due_sources(
            db,
            [ConfiguredConnector(source_type, configured=False)],
            now=now + timedelta(seconds=601),
        ) == []


def test_queue_claim_can_be_released_without_counting_as_provider_failure(monkeypatch):
    source_type = "qa_schedule_queue_release"
    _reset(source_type)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()
        claim = claim_due_sources(db, [ConfiguredConnector(source_type)], now=now)[0]
        assert release_source_claim(
            db, source_type, claim["lease_token"], delay_seconds=45, now=now
        )
        state = db.get(AcquisitionSourceState, source_type)
        assert state.last_status == "queue_failed"
        assert state.consecutive_failures == 0
        assert state.lease_token is None
        assert state.next_run_at.replace(tzinfo=UTC) == now + timedelta(seconds=45)


def test_scheduler_dispatches_independent_source_jobs_and_respects_durable_lease(monkeypatch):
    from app import scheduler

    source_type = "qa_schedule_dispatch"
    _reset(source_type)
    now = datetime.now(UTC)
    monkeypatch.setattr(scheduler, "connectors", lambda: [ConfiguredConnector(source_type)])

    class FakeQueue:
        def __init__(self):
            self.jobs = {}
            self.enqueued = []

        def fetch_job(self, job_id):
            return self.jobs.get(job_id)

        def enqueue(self, *args, **kwargs):
            job = type("Job", (), {"id": kwargs["job_id"]})()
            self.jobs[kwargs["job_id"]] = job
            self.enqueued.append((args, kwargs))
            return job

    queue = FakeQueue()
    assert scheduler._schedule_due_source_jobs(None, queue) == 0
    with SessionLocal() as db:
        state = db.get(AcquisitionSourceState, source_type)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()

    assert scheduler._schedule_due_source_jobs(None, queue) == 1
    assert len(queue.enqueued) == 1
    args, kwargs = queue.enqueued[0]
    assert args[:3] == ("app.worker.ingest_all_sources", "scheduled", source_type)
    assert len(args) == 4 and isinstance(args[3], str) and args[3]
    with SessionLocal() as db:
        state = db.get(AcquisitionSourceState, source_type)
        assert state.lease_token == args[3]
    assert kwargs["job_id"] == f"cignal-ingestion-source-{source_type}"
    assert kwargs["job_timeout"] >= 300
    assert scheduler._schedule_due_source_jobs(None, queue) == 0
    assert len(queue.enqueued) == 1


def test_scheduler_releases_claim_when_rq_enqueue_fails(monkeypatch):
    from app import scheduler

    source_type = "qa_schedule_dispatch_failure"
    _reset(source_type)
    now = datetime.now(UTC)
    monkeypatch.setattr(scheduler, "connectors", lambda: [ConfiguredConnector(source_type)])

    class FailingQueue:
        def fetch_job(self, job_id):
            return None

        def enqueue(self, *args, **kwargs):
            raise RuntimeError("queue unavailable")

    assert scheduler._schedule_due_source_jobs(None, FailingQueue()) == 0
    with SessionLocal() as db:
        state = db.get(AcquisitionSourceState, source_type)
        state.next_run_at = now - timedelta(seconds=1)
        db.commit()

    scheduler._schedule_due_source_jobs(None, FailingQueue())
    with SessionLocal() as db:
        state = db.get(AcquisitionSourceState, source_type)
        assert state.lease_token is None
        assert state.lease_expires_at is None
        assert state.last_status == "queue_failed"
        assert state.consecutive_failures == 0
        assert state.next_run_at.replace(tzinfo=UTC) > now


def test_global_manual_run_does_not_clobber_an_active_scheduled_lease(monkeypatch):
    from app.services.acquisition_schedule import mark_source_started

    source_type = "qa_schedule_manual_overlap"
    _reset(source_type)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with SessionLocal() as db:
        state = ensure_source_state(db, source_type, now=now)
        state.lease_token = "scheduled-owner"
        state.lease_expires_at = now + timedelta(minutes=10)
        state.last_status = "queued"
        db.commit()

        assert mark_source_started(db, source_type, now=now + timedelta(seconds=1))
        assert complete_source_run(
            db, source_type=source_type, status="succeeded", run_id="manual-run",
            now=now + timedelta(seconds=2),
        ) is False
        state = db.get(AcquisitionSourceState, source_type)
        assert state.lease_token == "scheduled-owner"
        assert state.last_status == "queued"
        assert state.last_run_id is None
