from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db.models import BackgroundJobOutbox, Base, EventDiscoveryRun, Report
from app.services.event_discovery_schedule import schedule_continuous_event_discovery


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory()


def _report(db, report_id, source_type, source_name, *, now, updated_seconds_ago=300,
            observed_hours_ago=1, analyzed=True, disaster_type="flood", longitude=72.5714):
    db.add(Report(
        id=report_id,
        tracking_code=f"TRK-{report_id}",
        title="Flood water rising near the central market",
        description="Flood water is entering homes and residential roads near Ahmedabad central market.",
        disaster_type=disaster_type,
        urgency="high",
        status="analyzed" if analyzed else "submitted",
        latitude=23.0225,
        longitude=longitude,
        source_type=source_type,
        source_name=source_name,
        source_observed_at=now - timedelta(hours=observed_hours_ago),
        analysis_revision=1,
        analyzed_revision=1 if analyzed else 0,
        created_at=now - timedelta(hours=observed_hours_ago),
        updated_at=now - timedelta(seconds=updated_seconds_ago),
    ))
    db.flush()


def test_continuous_discovery_waits_for_quiet_period_and_multiple_origins():
    engine, db = _session()
    try:
        now = datetime.now(UTC)
        _report(db, "r1", "gdacs", "GDACS", now=now, updated_seconds_ago=300)
        assert schedule_continuous_event_discovery(db, now=now) is None
        _report(db, "r2", "usgs", "USGS", now=now, updated_seconds_ago=60)
        assert schedule_continuous_event_discovery(db, now=now) is None
        db.query(Report).filter_by(id="r2").update({"updated_at": now - timedelta(seconds=300)})
        db.commit()
        result = schedule_continuous_event_discovery(db, now=now)
        assert result is not None
        run = db.get(EventDiscoveryRun, result["run_id"])
        assert run.trigger_kind == "continuous"
        assert run.status == "queued"
        assert run.dedupe_key.startswith("continuous:")
        job = db.query(BackgroundJobOutbox).filter_by(job_id=result["job_id"]).one()
        assert job.status == "pending"
        assert job.function_path == "app.worker.discover_event_hypotheses"
        assert job.task_args[0] == run.id
    finally:
        db.close()
        engine.dispose()


def test_continuous_discovery_is_idempotent_for_same_report_generation():
    engine, db = _session()
    try:
        now = datetime.now(UTC)
        _report(db, "r1", "gdacs", "GDACS", now=now)
        _report(db, "r2", "usgs", "USGS", now=now)
        result = schedule_continuous_event_discovery(db, now=now)
        assert result is not None
        assert schedule_continuous_event_discovery(db, now=now) is None
        assert db.query(EventDiscoveryRun).count() == 1
        assert db.query(BackgroundJobOutbox).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_continuous_discovery_ignores_unanalyzed_and_unlocated_reports():
    engine, db = _session()
    try:
        now = datetime.now(UTC)
        _report(db, "r1", "gdacs", "GDACS", now=now)
        _report(db, "r2", "usgs", "USGS", now=now, analyzed=False)
        assert schedule_continuous_event_discovery(db, now=now) is None
        row = db.get(Report, "r2")
        row.analyzed_revision = 1
        row.latitude = None
        db.commit()
        assert schedule_continuous_event_discovery(db, now=now) is None
    finally:
        db.close()
        engine.dispose()


def test_continuous_discovery_respects_disabled_setting():
    engine, db = _session()
    try:
        now = datetime.now(UTC)
        _report(db, "r1", "gdacs", "GDACS", now=now)
        _report(db, "r2", "usgs", "USGS", now=now)
        assert schedule_continuous_event_discovery(db, now=now, enabled=False) is None
        assert db.query(EventDiscoveryRun).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_event_discovery_migration_columns_and_unique_dedupe_index_present():
    engine, db = _session()
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("event_discovery_runs")}
        assert {"trigger_kind", "dedupe_key"}.issubset(columns)
        unique_constraints = inspect(engine).get_unique_constraints("event_discovery_runs")
        assert any(item.get("column_names") == ["dedupe_key"] for item in unique_constraints)
    finally:
        db.close()
        engine.dispose()
