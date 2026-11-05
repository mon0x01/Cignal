from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.db.models import AcquisitionBackfillRun, AcquisitionObservation, Report
from app.db.session import SessionLocal, initialize_database
from app.integrations.base import NormalizedSignal


class HistoricalUSGS:
    source_type = "usgs"
    name = "Test USGS Historical Source"

    def configured(self):
        return True

    def fetch_window(self, start_at, end_at, *, offset=1, limit=500):
        return [NormalizedSignal(
            source_type="usgs", source_name=self.name, source_record_id="qa-backfill-usgs-1",
            title="Earthquake M4.1 near test region",
            description="USGS scientific earthquake observation for backfill worker test.",
            disaster_type="earthquake", urgency="moderate", latitude=23.0, longitude=72.5,
            location_text="Test region", source_url="https://earthquake.usgs.gov/test",
            provenance="historical", observed_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            source_metadata={"magnitude": 4.1},
        )]


def _new_run(db, *, source_record_id="qa-backfill-usgs-1"):
    run = AcquisitionBackfillRun(
        id=str(uuid4()), source_type="usgs",
        start_at=datetime(2026, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC),
        window_days=1, cursor_at=datetime(2026, 1, 1, tzinfo=UTC),
        page_offset=1, max_records=100, status="queued",
    )
    db.add(run)
    db.commit()
    return run


def test_historical_backfill_checkpoints_observations_and_is_idempotent(monkeypatch):
    import app.integrations.registry as registry
    import app.worker as worker

    initialize_database()
    with SessionLocal() as db:
        db.query(AcquisitionBackfillRun).delete()
        db.query(AcquisitionObservation).filter_by(source_type="usgs", source_record_id="qa-backfill-usgs-1").delete()
        db.query(Report).filter_by(source_type="usgs", source_record_id="qa-backfill-usgs-1").delete()
        db.commit()
        first = _new_run(db)
        first_id = first.id

    monkeypatch.setattr(registry, "connectors", lambda: [HistoricalUSGS()])
    monkeypatch.setattr(worker, "enqueue_job", lambda *args: SimpleNamespace(id="qa-job"))

    first_result = worker.run_acquisition_backfill(first_id)
    assert first_result["status"] == "completed", first_result
    assert first_result["records_inserted"] == 1
    assert first_result["observations_captured"] == 1

    with SessionLocal() as db:
        second = _new_run(db)
        second_id = second.id
    second_result = worker.run_acquisition_backfill(second_id)
    assert second_result["status"] == "completed"
    assert second_result["records_inserted"] == 0
    assert second_result["records_duplicate"] == 1

    with SessionLocal() as db:
        observation = db.query(AcquisitionObservation).filter_by(
            source_type="usgs", source_record_id="qa-backfill-usgs-1"
        ).one()
        assert observation.seen_count == 2
        assert observation.report_id is not None
        assert db.query(Report).filter_by(
            source_type="usgs", source_record_id="qa-backfill-usgs-1"
        ).count() == 1


def test_active_backfill_lease_rejects_second_worker(monkeypatch):
    import app.worker as worker

    initialize_database()
    with SessionLocal() as db:
        run = _new_run(db)
        run.status = "running"
        run.lease_token = str(uuid4())
        from datetime import timedelta
        run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        run_id = run.id
    result = worker.run_acquisition_backfill(run_id)
    assert result["reason"] == "backfill_already_running"


class HistoricalEONET:
    source_type = "nasa_eonet"
    name = "Test NASA EONET Historical Source"

    def configured(self):
        return True

    def fetch_window(self, start_at, end_at):
        return [
            NormalizedSignal(
                source_type=self.source_type, source_name=self.name,
                source_record_id=f"qa-eonet-{index}",
                title=f"Flood event update {index}",
                description=f"Curated flood event metadata record {index}.",
                disaster_type="flood", urgency="unknown",
                latitude=23.0 + index * 0.01, longitude=72.5 + index * 0.01,
                location_text="Test region", source_url=f"https://eonet.gsfc.nasa.gov/test/{index}",
                provenance="historical", observed_at=datetime(2026, 1, 1, 12, index, tzinfo=UTC),
                source_metadata={"eonet_event_id": f"EONET_{index}", "provider_payload_revision": "v1"},
            )
            for index in range(5)
        ]


def test_eonet_record_cap_resumes_inside_same_date_window_without_skipping(monkeypatch):
    import app.integrations.registry as registry
    import app.worker as worker

    initialize_database()
    with SessionLocal() as db:
        db.query(AcquisitionBackfillRun).delete()
        db.query(AcquisitionObservation).filter(AcquisitionObservation.source_record_id.like("qa-eonet-%")).delete(synchronize_session=False)
        db.query(Report).filter(Report.source_record_id.like("qa-eonet-%")).delete(synchronize_session=False)
        db.commit()
        run = AcquisitionBackfillRun(
            id=str(uuid4()), source_type="nasa_eonet",
            start_at=datetime(2026, 1, 1, tzinfo=UTC), end_at=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC),
            window_days=1, cursor_at=datetime(2026, 1, 1, tzinfo=UTC), page_offset=1,
            max_records=2, status="queued",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    monkeypatch.setattr(registry, "connectors", lambda: [HistoricalEONET()])
    monkeypatch.setattr(worker, "enqueue_job", lambda *args: SimpleNamespace(id="qa-eonet-job"))

    first = worker.run_acquisition_backfill(run_id)
    assert first["status"] == "limit_reached"
    assert first["records_inserted"] == 2
    with SessionLocal() as db:
        run = db.get(AcquisitionBackfillRun, run_id)
        assert run.page_offset == 3
        assert run.cursor_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=UTC)
        run.max_records = 5
        run.status = "queued"
        db.commit()

    resumed = worker.run_acquisition_backfill(run_id)
    assert resumed["status"] == "completed"
    assert resumed["records_inserted"] == 5
    with SessionLocal() as db:
        assert db.query(Report).filter(Report.source_record_id.like("qa-eonet-%")).count() == 5
        assert db.query(AcquisitionObservation).filter(
            AcquisitionObservation.source_record_id.like("qa-eonet-%")
        ).count() == 5


def test_backfill_recovers_reports_when_analysis_queue_was_unavailable(monkeypatch):
    import app.integrations.registry as registry
    import app.worker as worker

    initialize_database()
    with SessionLocal() as db:
        db.query(AcquisitionBackfillRun).delete()
        db.query(AcquisitionObservation).filter(
            AcquisitionObservation.source_type == "usgs",
            AcquisitionObservation.source_record_id.like("qa-backfill-%"),
        ).delete(synchronize_session=False)
        db.query(Report).filter(
            Report.source_type == "usgs",
            Report.source_record_id.like("qa-backfill-%"),
        ).delete(synchronize_session=False)
        db.commit()
        run = _new_run(db)
        run_id = run.id

    class QueueRecoveryUSGS(HistoricalUSGS):
        def fetch_window(self, start_at, end_at, *, offset=1, limit=500):
            signal = super().fetch_window(start_at, end_at, offset=offset, limit=limit)[0]
            signal.source_record_id = "qa-backfill-queue-recovery"
            return [signal] if offset == 1 else []

    monkeypatch.setattr(registry, "connectors", lambda: [QueueRecoveryUSGS()])
    calls = {"count": 0}

    def unavailable_then_available(*args):
        calls["count"] += 1
        return None if calls["count"] <= 2 else SimpleNamespace(id="qa-recovered-job")

    monkeypatch.setattr(worker, "enqueue_job", unavailable_then_available)
    first = worker.run_acquisition_backfill(run_id)
    assert first["status"] == "partial"
    assert first["analysis_queue_failures"] == 1

    with SessionLocal() as db:
        run = db.get(AcquisitionBackfillRun, run_id)
        run.status = "queued"
        db.commit()

    second = worker.run_acquisition_backfill(run_id)
    assert second["status"] == "completed", second
    assert second["analysis_queue_failures"] == 0
    assert second["records_inserted"] == 1
    assert calls["count"] == 3
