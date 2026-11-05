from __future__ import annotations


def test_0014_acquisition_schedule_migration_is_idempotent_and_additive():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE acquisition_source_states")
        connection.exec_driver_sql("DROP TABLE integration_runs")
        connection.exec_driver_sql(
            "CREATE TABLE integration_runs (id VARCHAR(36) PRIMARY KEY, source_type VARCHAR(24))"
        )
        connection.execute(
            text("INSERT INTO integration_runs (id, source_type) VALUES ('keep-run', 'usgs')")
        )
        migration = importlib.import_module("migrations.versions.0014_acquisition_source_scheduler")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert inspector.has_table("acquisition_source_states")
        columns = {item["name"] for item in inspector.get_columns("acquisition_source_states")}
        assert {
            "source_type", "poll_interval_seconds", "next_run_at", "lease_token",
            "lease_expires_at", "last_status", "consecutive_failures", "last_run_id",
        }.issubset(columns)
        indexes = {item["name"] for item in inspector.get_indexes("acquisition_source_states")}
        assert "ix_acquisition_source_due" in indexes
        assert connection.execute(
            text("SELECT id FROM integration_runs WHERE id='keep-run'")
        ).scalar_one() == "keep-run"
    engine.dispose()


def test_integration_api_exposes_schedule_and_source_specific_operations():
    from app.api.main import app

    routes = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/integrations/schedules", "GET") in routes
    assert ("/api/integrations/ingest/{source_type}", "POST") in routes
