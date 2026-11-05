from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import AcquisitionSourceState, IntegrationRun, Report
from app.db.session import SessionLocal, initialize_database
from app.integrations.base import NormalizedSignal


class FakeConnector:
    def __init__(self, source_type: str, records: list[NormalizedSignal]):
        self.name = f"QA {source_type}"
        self.source_type = source_type
        self.records = records

    def configured(self):
        return True

    def fetch(self):
        return self.records


def _signal(source_type: str, record_id: str) -> NormalizedSignal:
    return NormalizedSignal(
        source_type=source_type,
        source_name="QA connector",
        source_record_id=record_id,
        title="Flood warning near the river",
        description="Water level is rising near residential streets.",
        disaster_type="flood",
        urgency="high",
        latitude=23.0225,
        longitude=72.5714,
        location_text="Ahmedabad, Gujarat",
        source_url="https://example.invalid/event/1",
        provenance="live",
        observed_at=datetime.now(UTC),
    )


def _prepare(monkeypatch, connectors):
    from app import worker
    from app.integrations import registry

    initialize_database()
    source_types = [item.source_type for item in connectors]
    with SessionLocal() as db:
        db.query(Report).filter(Report.source_type.in_(source_types)).delete(synchronize_session=False)
        db.query(IntegrationRun).filter(IntegrationRun.source_type.in_(source_types)).delete(synchronize_session=False)
        db.query(AcquisitionSourceState).filter(AcquisitionSourceState.source_type.in_(source_types)).delete(synchronize_session=False)
        db.commit()
    monkeypatch.setattr(registry, "connectors", lambda: connectors)
    monkeypatch.setattr(worker, "publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "enqueue_job", lambda *_args, **_kwargs: object())
    return worker


def test_source_scoped_ingestion_does_not_run_unrelated_connectors(monkeypatch):
    selected_type = "qa_scoped_selected"
    unrelated_type = "qa_scoped_unrelated"
    selected = FakeConnector(selected_type, [_signal(selected_type, "selected-001")])
    unrelated = FakeConnector(unrelated_type, [_signal(unrelated_type, "unrelated-001")])
    worker = _prepare(monkeypatch, [selected, unrelated])

    result = worker.ingest_all_sources(trigger="manual", source_type=selected_type)

    assert result["ok"] is True
    assert result["source_type"] == selected_type
    assert result["inserted"] == 1
    with SessionLocal() as db:
        reports = db.query(Report).filter(
            Report.source_type.in_([selected_type, unrelated_type])
        ).all()
        assert [report.source_type for report in reports] == [selected_type]
        runs = db.query(IntegrationRun).filter(
            IntegrationRun.source_type.in_([selected_type, unrelated_type])
        ).all()
        assert [run.source_type for run in runs] == [selected_type]


def test_source_scoped_ingestion_rejects_stale_scheduler_lease(monkeypatch):
    source_type = "qa_scoped_stale_lease"
    connector = FakeConnector(source_type, [_signal(source_type, "must-not-fetch")])
    worker = _prepare(monkeypatch, [connector])
    from app.services.acquisition_schedule import ensure_source_state

    with SessionLocal() as db:
        state = ensure_source_state(db, source_type)
        state.lease_token = "newer-token"
        state.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        db.commit()

    result = worker.ingest_all_sources(
        trigger="scheduled", source_type=source_type, lease_token="stale-token"
    )

    assert result["ok"] is True
    assert result["inserted"] == 0
    assert result["source_runs"] == [{
        "source_type": source_type,
        "status": "skipped",
        "reason": "stale_or_expired_lease",
    }]
    with SessionLocal() as db:
        assert db.query(Report).filter_by(source_type=source_type).count() == 0
        state = db.get(AcquisitionSourceState, source_type)
        assert state.lease_token == "newer-token"
