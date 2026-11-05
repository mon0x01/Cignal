import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_0013_source_provenance_migration_is_idempotent_and_preserves_existing_rows():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE reports (id VARCHAR(36) PRIMARY KEY, title VARCHAR(180))")
        connection.execute(text("INSERT INTO reports (id, title) VALUES ('existing', 'Old report')"))
        migration = importlib.import_module("migrations.versions.0013_source_provenance")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        columns = {item["name"]: item for item in inspect(connection).get_columns("reports")}
        assert "source_metadata" in columns
        assert columns["source_metadata"]["nullable"] is False
        assert connection.execute(
            text("SELECT title, source_metadata FROM reports WHERE id='existing'")
        ).one() == ("Old report", "{}")
    engine.dispose()
