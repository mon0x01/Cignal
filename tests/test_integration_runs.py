from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models import IntegrationRun
from app.db.session import SessionLocal, initialize_database
from app.integrations.base import NormalizedSignal
from app.services.integration_runs import connector_health, integration_run_dict


class FakeConnector:
    def __init__(self, source_type: str, fetch_result=None, error: Exception | None = None, configured: bool = True):
        self.name = f"QA {source_type}"
        self.source_type = source_type
        self.fetch_result = fetch_result or []
        self.error = error
        self.is_configured = configured

    def configured(self):
        return self.is_configured

    def fetch(self):
        if self.error:
            raise self.error
        return self.fetch_result


def _signal(source_type: str, record_id: str, **changes):
    data = {
        "source_type": source_type,
        "source_name": "Ignored provider label",
        "source_record_id": record_id,
        "title": "Flood warning near the river",
        "description": "Water level is rising near residential streets.",
        "disaster_type": "flood",
        "urgency": "high",
        "latitude": 23.0225,
        "longitude": 72.5714,
        "location_text": "Ahmedabad, Gujarat",
        "source_url": "https://example.invalid/event/1",
        "provenance": "live",
        "observed_at": datetime.now(UTC),
    }
    data.update(changes)
    return NormalizedSignal(**data)


def _prepare(monkeypatch, connectors, queue_result=True):
    from app import worker
    from app.integrations import registry

    initialize_database()
    # Isolate QA connector/backlog rows created by this test module.
    with SessionLocal() as db:
        from app.db.models import Report
        db.query(Report).filter(Report.analysis_revision > Report.analyzed_revision).delete(synchronize_session=False)
        db.query(IntegrationRun).delete(synchronize_session=False)
        db.commit()
    queued = []
    jobs = {}
    monkeypatch.setattr(registry, "connectors", lambda: connectors)
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)

    def enqueue(*args, **_kwargs):
        report_id = args[1] if len(args) > 1 and args[0] == "app.worker.process_report" else None
        if report_id in jobs:
            return jobs[report_id]
        if not queue_result:
            return None
        job = object()
        if report_id:
            jobs[report_id] = job
        queued.append(args)
        return job

    monkeypatch.setattr(worker, "enqueue_job", enqueue)
    return worker, queued


def test_successful_ingest_persists_metrics_and_reuses_pending_analysis_job(monkeypatch):
    source = "qa_run_ok"
    observed_at = datetime.now(UTC) - timedelta(hours=5)
    connector = FakeConnector(source, [_signal(source, "provider-001", observed_at=observed_at)])
    worker, queued = _prepare(monkeypatch, [connector])

    first = worker.ingest_all_sources(trigger="manual")
    second = worker.ingest_all_sources(trigger="manual")

    assert first["ok"] is True
    assert first["inserted"] == 1
    assert first["queued"] == 1
    assert second["inserted"] == 0
    assert second["duplicates"] == 1
    assert second["ok"] is True
    assert second["queued"] == 1  # Existing deterministic job is reused, not duplicated.
    assert len(queued) == 1

    with SessionLocal() as db:
        runs = db.query(IntegrationRun).filter_by(source_type=source).order_by(IntegrationRun.started_at).all()
        assert len(runs) == 2
        assert runs[0].status == "succeeded"
        assert runs[0].trigger == "manual"
        assert runs[0].records_fetched == 1
        assert runs[0].fetch_attempts == 1
        assert runs[0].records_inserted == 1
        assert runs[0].records_queued == 1
        from app.db.models import Report
        stored = db.query(Report).filter_by(source_type=source).one()
        assert stored.source_observed_at.replace(tzinfo=UTC) == observed_at
        assert runs[1].status == "succeeded"
        assert runs[1].records_duplicate == 1
        assert runs[1].records_queued == 1


def test_bad_record_is_rejected_without_discarding_valid_records(monkeypatch):
    source = "qa_run_partial"
    connector = FakeConnector(source, [
        _signal("spoofed_source", "wrong-source"),
        _signal(source, "invalid-coordinates", latitude=91.0),
        _signal(source, "valid-record"),
    ])
    worker, queued = _prepare(monkeypatch, [connector])

    result = worker.ingest_all_sources()

    assert result["ok"] is True
    assert result["inserted"] == 1
    assert result["rejected"] == 2
    assert result["queued"] == 1
    with SessionLocal() as db:
        run = db.query(IntegrationRun).filter_by(source_type=source).one()
        assert run.status == "partial"
        assert run.records_fetched == 3
        assert run.rejection_counts == {"source_type_mismatch": 1, "invalid_coordinates": 1}


def test_transient_connector_failure_is_recorded_and_other_connectors_continue(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "integration_retry_attempts", 1)
    failed = FakeConnector("qa_run_timeout", error=TimeoutError("private endpoint token should not persist"))
    working = FakeConnector("qa_run_after_error", [_signal("qa_run_after_error", "works")])
    worker, queued = _prepare(monkeypatch, [failed, working])

    result = worker.ingest_all_sources()

    assert result["ok"] is False
    assert result["inserted"] == 1
    assert len(result["failures"]) == 1
    assert result["failures"][0]["retryable"] is True
    with SessionLocal() as db:
        failed_run = db.query(IntegrationRun).filter_by(source_type="qa_run_timeout").one()
        good_run = db.query(IntegrationRun).filter_by(source_type="qa_run_after_error").one()
        assert failed_run.status == "failed"
        assert failed_run.failure_code == "TimeoutError"
        assert failed_run.retryable is True
        assert failed_run.fetch_attempts == 1
        assert "private endpoint token" not in str(integration_run_dict(failed_run))
        assert good_run.status == "succeeded"


def test_empty_feed_is_healthy_empty_not_failed(monkeypatch):
    source = "qa_run_empty"
    worker, _queued = _prepare(monkeypatch, [FakeConnector(source, [])])
    result = worker.ingest_all_sources()
    assert result["ok"] is True
    with SessionLocal() as db:
        run = db.query(IntegrationRun).filter_by(source_type=source).one()
        assert run.status == "empty"
        assert run.records_fetched == 0
        assert run.failure_code is None


def test_queue_unavailable_marks_run_partial_without_losing_imported_report(monkeypatch):
    source = "qa_run_queue"
    connector = FakeConnector(source, [_signal(source, "queue-outage")])
    worker, _queued = _prepare(monkeypatch, [connector], queue_result=False)

    result = worker.ingest_all_sources()

    assert result["ok"] is False
    assert result["inserted"] == 1
    assert result["queue_failures"] == 1
    with SessionLocal() as db:
        run = db.query(IntegrationRun).filter_by(source_type=source).one()
        assert run.status == "partial"
        assert run.records_inserted == 1
        assert run.queue_failures == 1


def test_ingestion_retries_unqueued_citizen_reports_in_global_backlog(monkeypatch):
    source = "qa_run_backlog"
    connector = FakeConnector(source, [])
    worker, queued = _prepare(monkeypatch, [connector], queue_result=False)
    from app.db.models import Report

    with SessionLocal() as db:
        report = Report(
            id=str(uuid4()), tracking_code=f"CIG-R-{uuid4().hex[:24].upper()}",
            title="Citizen report awaiting AI analysis", description="Residents report flood water entering homes near the local road.",
            disaster_type="flood", urgency="high", status="submitted", language="en",
            source_type="citizen", source_name="Citizen submission", provenance="submitted",
        )
        db.add(report)
        db.commit()
        report_id = report.id

    failed = worker.ingest_all_sources()
    assert failed["backlog_queue_failures"] == 1
    assert failed["ok"] is False

    # Redis is available on the next scheduled pass; the previously pending item is retried.
    from app import worker
    queued = []
    monkeypatch.setattr(worker, "enqueue_job", lambda *args, **_kwargs: queued.append(args) or object())
    recovered = worker.ingest_all_sources()
    assert recovered["backlog_queued"] == 1
    assert queued == [("app.worker.process_report", report_id, 1)]


def test_unconfigured_source_is_not_run_or_reported_as_failed(monkeypatch):
    source = "qa_run_unconfigured"
    worker, _queued = _prepare(monkeypatch, [FakeConnector(source, configured=False)])
    result = worker.ingest_all_sources()
    assert result["ok"] is True
    assert result["source_runs"] == []
    with SessionLocal() as db:
        assert db.query(IntegrationRun).filter_by(source_type=source).count() == 0


def test_connector_health_distinguishes_never_run_from_failed_and_healthy():
    initialize_database()
    with SessionLocal() as db:
        now = datetime.now(UTC)
        db.add_all([
            IntegrationRun(
                id=str(uuid4()), source_type="qa_health_failed", source_name="Failed feed",
                trigger="scheduled", status="failed", started_at=now - timedelta(minutes=2),
                completed_at=now - timedelta(minutes=1), failure_code="TimeoutError", retryable=True,
            ),
            IntegrationRun(
                id=str(uuid4()), source_type="qa_health_good", source_name="Working feed",
                trigger="scheduled", status="succeeded", started_at=now - timedelta(minutes=2),
                completed_at=now - timedelta(minutes=1), records_inserted=3,
            ),
        ])
        db.commit()
        rows = connector_health(db, [
            {"name": "Never", "source_type": "qa_health_never", "configured": True},
            {"name": "Failed", "source_type": "qa_health_failed", "configured": True},
            {"name": "Good", "source_type": "qa_health_good", "configured": True},
            {"name": "No config", "source_type": "qa_health_config", "configured": False},
        ])
    assert [row["health_status"] for row in rows] == ["not_tested", "error", "healthy", "needs_configuration"]
    assert rows[2]["inserted_last_24h"] == 3


def test_integration_run_dict_does_not_expose_raw_exception_messages():
    run = IntegrationRun(
        id="run", source_type="qa", source_name="QA", trigger="manual", status="failed",
        failure_code="ConnectError", retryable=True,
        started_at=datetime.now(UTC), rejection_counts={"invalid_coordinates": 1},
    )
    serialized = integration_run_dict(run)
    assert serialized["failure_code"] == "ConnectError"
    assert serialized["retryable"] is True
    assert serialized["fetch_attempts"] == 1
    assert "error_message" not in serialized


def test_temporal_correlation_prefers_source_observed_time_over_ingestion_time():
    from app.intelligence.correlation import score_report_pair

    ingested_now = datetime.now(UTC)
    event_time = ingested_now - timedelta(days=20)
    report = {
        "id": "source-report", "title": "Earthquake near district center",
        "description": "Ground shaking reported in nearby buildings.",
        "disaster_type": "earthquake", "latitude": 23.0, "longitude": 72.0,
        "created_at": ingested_now, "source_observed_at": event_time,
    }
    candidate = {
        "id": "candidate", "title": "Earthquake near district center",
        "description": "Ground shaking reported in nearby buildings.",
        "disaster_type": "earthquake", "latitude": 23.0, "longitude": 72.0,
        "created_at": ingested_now, "source_observed_at": event_time - timedelta(hours=2),
    }
    score = score_report_pair(report, candidate)
    assert score["age_hours"] == 2.0
    assert score["temporal_score"] == 1.0


def test_provider_timestamp_parser_normalizes_iso_epoch_and_rss_struct_time():
    import calendar
    import time

    from app.integrations.base import parse_datetime_utc

    iso = parse_datetime_utc("2026-11-04T10:15:00+05:30")
    assert iso == datetime(2026, 11, 4, 4, 45, tzinfo=UTC)
    assert parse_datetime_utc("2026-11-04T04:45:00") == iso
    epoch_ms = calendar.timegm((2026, 11, 4, 4, 45, 0)) * 1000
    assert parse_datetime_utc(epoch_ms) == iso
    rss = parse_datetime_utc(time.gmtime(epoch_ms / 1000))
    assert rss == iso
    assert parse_datetime_utc("not-a-provider-timestamp") is None


def test_ingestion_keeps_record_when_optional_provider_timestamp_is_malformed(monkeypatch):
    source = "qa_run_bad_time"
    connector = FakeConnector(source, [
        _signal(source, "bad-time", observed_at="not-a-date"),
    ])
    worker, _queued = _prepare(monkeypatch, [connector])

    result = worker.ingest_all_sources()

    assert result["inserted"] == 1
    with SessionLocal() as db:
        from app.db.models import Report
        report = db.query(Report).filter_by(source_type=source).one()
        assert report.source_observed_at is None
        assert db.query(IntegrationRun).filter_by(source_type=source).one().status == "succeeded"


def test_source_health_marks_old_success_as_stale():
    initialize_database()
    old_time = datetime.now(UTC) - timedelta(days=3)
    with SessionLocal() as db:
        db.add(IntegrationRun(
            id=str(uuid4()), source_type="qa_health_stale", source_name="Stale feed",
            trigger="scheduled", status="succeeded", started_at=old_time,
            completed_at=old_time, records_inserted=2,
        ))
        db.commit()
        rows = connector_health(db, [{
            "name": "Stale", "source_type": "qa_health_stale", "configured": True,
        }])
    assert rows[0]["health_status"] == "stale"


def test_source_health_reports_active_and_abandoned_running_jobs():
    initialize_database()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.add_all([
            IntegrationRun(
                id=str(uuid4()), source_type="qa_health_running", source_name="Active feed",
                trigger="scheduled", status="running", started_at=now - timedelta(minutes=5),
            ),
            IntegrationRun(
                id=str(uuid4()), source_type="qa_health_abandoned", source_name="Abandoned feed",
                trigger="scheduled", status="running", started_at=now - timedelta(hours=3),
            ),
        ])
        db.commit()
        rows = connector_health(db, [
            {"name": "Active", "source_type": "qa_health_running", "configured": True},
            {"name": "Abandoned", "source_type": "qa_health_abandoned", "configured": True},
        ])
    assert [row["health_status"] for row in rows] == ["running", "stale"]


def _install_fake_rq(monkeypatch, existing_job=None):
    import sys
    import types

    calls = {"fetch_ids": [], "enqueued": [], "connection_closed": False}

    class FakeConnection:
        def close(self):
            calls["connection_closed"] = True

    class FakeRedis:
        @staticmethod
        def from_url(_url):
            return FakeConnection()

    class FakeQueue:
        def __init__(self, name, connection):
            assert name == "cignal"
            self.connection = connection

        def fetch_job(self, job_id):
            calls["fetch_ids"].append(job_id)
            return existing_job

        def enqueue(self, function_path, *args, **kwargs):
            calls["enqueued"].append((function_path, args, kwargs))
            return types.SimpleNamespace(id=kwargs["job_id"])

    redis_module = types.ModuleType("redis")
    redis_module.Redis = FakeRedis
    rq_module = types.ModuleType("rq")
    rq_module.Queue = FakeQueue
    monkeypatch.setitem(sys.modules, "redis", redis_module)
    monkeypatch.setitem(sys.modules, "rq", rq_module)
    return calls


def test_analysis_enqueue_uses_stable_report_specific_job_id(monkeypatch):
    calls = _install_fake_rq(monkeypatch)
    from app.services.events import enqueue_job

    job = enqueue_job("app.worker.process_report", "report-123", 7)

    expected_id = "cignal-report-analysis-report-123-r7"
    assert job.id == expected_id
    assert calls["fetch_ids"] == [expected_id]
    assert calls["enqueued"] == [(
        "app.worker.process_report",
        ("report-123", 7),
        {"job_id": expected_id, "job_timeout": 300, "result_ttl": 3600},
    )]
    assert calls["connection_closed"] is True


def test_analysis_enqueue_reuses_existing_active_job(monkeypatch):
    class ExistingJob:
        def __init__(self):
            self.deleted = False

        def get_status(self, refresh=False):
            assert refresh is True
            return "started"

        def delete(self):
            self.deleted = True

    existing = ExistingJob()
    calls = _install_fake_rq(monkeypatch, existing_job=existing)
    from app.services.events import enqueue_job

    result = enqueue_job("app.worker.process_report", "report-456", 2)

    assert result is existing
    assert existing.deleted is False
    assert calls["enqueued"] == []


def test_analysis_enqueue_replaces_failed_job(monkeypatch):
    class FailedJob:
        def __init__(self):
            self.deleted = False

        def get_status(self, refresh=False):
            assert refresh is True
            return "failed"

        def delete(self):
            self.deleted = True

    failed = FailedJob()
    calls = _install_fake_rq(monkeypatch, existing_job=failed)
    from app.services.events import enqueue_job

    result = enqueue_job("app.worker.process_report", "report-789", 4)

    assert failed.deleted is True
    assert result.id == "cignal-report-analysis-report-789-r4"
    assert len(calls["enqueued"]) == 1


def test_ingestion_enqueue_reuses_active_global_job(monkeypatch):
    class ExistingJob:
        def get_status(self, refresh=False):
            assert refresh is True
            return "queued"

        def delete(self):
            raise AssertionError("An active ingestion job must not be deleted")

    existing = ExistingJob()
    calls = _install_fake_rq(monkeypatch, existing_job=existing)
    from app.services.events import enqueue_job

    result = enqueue_job("app.worker.ingest_all_sources", "manual")

    assert result is existing
    assert calls["fetch_ids"] == ["cignal-ingestion-all-sources"]
    assert calls["enqueued"] == []


def test_ingestion_enqueue_replaces_completed_global_job_for_next_cycle(monkeypatch):
    class CompletedJob:
        def __init__(self):
            self.deleted = False

        def get_status(self, refresh=False):
            assert refresh is True
            return "finished"

        def delete(self):
            self.deleted = True

    completed = CompletedJob()
    calls = _install_fake_rq(monkeypatch, existing_job=completed)
    from app.services.events import enqueue_job

    result = enqueue_job("app.worker.ingest_all_sources", "scheduled")

    assert completed.deleted is True
    assert result.id == "cignal-ingestion-all-sources"
    assert calls["enqueued"][0][2]["job_timeout"] == 900


def test_connector_fetch_retry_success_is_recorded_in_run_telemetry(monkeypatch):
    from app.core.config import settings
    from app.integrations import retry

    source = "qa_run_retry_success"
    connector = FakeConnector(source, [_signal(source, "event-after-timeout")])
    calls = {"fetch": 0}

    def fetch_then_succeed():
        calls["fetch"] += 1
        if calls["fetch"] < 3:
            raise TimeoutError("temporary provider timeout")
        return connector.fetch_result

    connector.fetch = fetch_then_succeed
    monkeypatch.setattr(settings, "integration_retry_attempts", 3)
    monkeypatch.setattr(retry.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: 0)
    worker, _queued = _prepare(monkeypatch, [connector])

    result = worker.ingest_all_sources()

    assert result["inserted"] == 1
    with SessionLocal() as db:
        run = db.query(IntegrationRun).filter_by(source_type=source).one()
        assert run.status == "succeeded"
        assert run.fetch_attempts == 3
        assert integration_run_dict(run)["fetch_attempts"] == 3




def test_connector_retry_log_binds_source_identity_for_each_connector(monkeypatch):
    from app import worker
    from app.core.config import settings
    from app.integrations import retry

    class RetryOnceConnector(FakeConnector):
        def __init__(self, source):
            super().__init__(
                source,
                [_signal(source, f"event-{source}")],
            )
            self.calls = 0

        def fetch(self):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary provider timeout")
            return self.fetch_result

    sources = ("qa_retry_alpha", "qa_retry_beta")
    connectors = [
        RetryOnceConnector(source)
        for source in sources
    ]

    monkeypatch.setattr(settings, "integration_retry_attempts", 2)
    monkeypatch.setattr(retry.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(retry.random, "uniform", lambda low, high: 0.0)

    retry_logs = []

    def capture_warning(message, *args, **kwargs):
        retry_logs.append((message, args))

    monkeypatch.setattr(worker.logger, "warning", capture_warning)

    worker_module, _queued = _prepare(monkeypatch, connectors)

    result = worker_module.ingest_all_sources()

    assert result["ok"] is True

    # Confirm that each connector failed once and then succeeded.
    assert {
        connector.source_type: connector.calls
        for connector in connectors
    } == {
        source: 2
        for source in sources
    }

    # Inspect the actual logger arguments, not pytest log handlers.
    retry_records = [
        args
        for message, args in retry_logs
        if message.startswith("Retrying connector ")
    ]

    assert len(retry_records) == 2
    assert {
        args[0]
        for args in retry_records
    } == set(sources)




def test_uncommitted_partial_batch_is_not_reported_as_inserted(monkeypatch):
    from app.core.config import settings

    source = "qa_run_partial_commit"
    connector = FakeConnector(source, [
        _signal(source, "first-in-uncommitted-batch"),
        _signal(source, "will-fail-normalization"),
    ])
    monkeypatch.setattr(settings, "integration_retry_attempts", 1)
    worker, _queued = _prepare(monkeypatch, [connector])
    original_normalize = worker._normalize_ingested_signal

    def fail_second(signal, selected_connector):
        if signal.source_record_id == "will-fail-normalization":
            raise RuntimeError("synthetic normalization subsystem failure")
        return original_normalize(signal, selected_connector)

    monkeypatch.setattr(worker, "_normalize_ingested_signal", fail_second)
    result = worker.ingest_all_sources()

    assert result["ok"] is False
    with SessionLocal() as db:
        run = db.query(IntegrationRun).filter_by(source_type=source).one()
        from app.db.models import Report
        assert run.status == "failed"
        assert run.records_fetched == 2
        assert run.records_inserted == 0
        assert run.fetch_attempts == 1
        assert db.query(Report).filter_by(source_type=source).count() == 0
