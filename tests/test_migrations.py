from __future__ import annotations

import pytest
from sqlalchemy import inspect, text


def test_alembic_migrations_create_schema_and_encrypted_mfa_columns():
    from app.db.session import engine, initialize_database

    initialize_database()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "users", "reports", "evidence", "incidents", "report_incidents", "audit_logs",
        "incident_activities", "integration_runs", "event_hypotheses",
        "event_hypothesis_reports", "event_discovery_runs", "acquisition_source_states",
        "acquisition_observations", "acquisition_backfill_runs", "acquisition_backfill_items", "pair_candidate_mining_runs",
        "event_pair_candidates", "model_evaluation_runs", "incident_actions", "incident_action_events",
        "incident_response_plans", "incident_action_dependencies", "background_job_outbox",
    }.issubset(tables)
    user_columns = {column["name"]: column for column in inspector.get_columns("users")}
    report_columns = {column["name"]: column for column in inspector.get_columns("reports")}
    action_checks = {item.get("name") for item in inspector.get_check_constraints("incident_actions")}
    assert {
        "ck_incident_actions_version_positive", "ck_incident_actions_type",
        "ck_incident_actions_priority", "ck_incident_actions_status",
        "ck_incident_actions_approval_status", "ck_incident_actions_approval_gate",
    }.issubset(action_checks)
    assert user_columns["mfa_secret"]["type"].length == 255
    assert user_columns["pending_mfa_secret"]["type"].length == 255
    assert report_columns["tracking_code"]["type"].length == 32
    assert "ai_analysis" in report_columns
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    run_columns = {column["name"]: column for column in inspector.get_columns("integration_runs")}
    assert {"source_type", "status", "records_fetched", "records_inserted", "records_duplicate", "records_rejected", "retryable"}.issubset(run_columns)
    assert version == "0019_continuous_event_discovery"
    assert len(version) <= 32
    assert "source_observed_at" in report_columns
    assert {"analysis_revision", "analyzed_revision"}.issubset(report_columns)
    assert "ix_reports_analysis_pending" in {item["name"] for item in inspector.get_indexes("reports")}
    assert "fetch_attempts" in run_columns
    action_columns = {column["name"] for column in inspector.get_columns("incident_actions")}
    assert {"response_plan_id", "sequence_order"}.issubset(action_columns)


def test_0019_migration_adds_continuous_event_discovery_metadata_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine("sqlite:///:memory:")
    migration = importlib.import_module("migrations.versions.0019_continuous_event_discovery")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE event_discovery_runs (id VARCHAR(36) PRIMARY KEY, status VARCHAR(16), created_at DATETIME)"
        )
        connection.execute(
            text("INSERT INTO event_discovery_runs (id, status, created_at) VALUES ('legacy-run', 'completed', CURRENT_TIMESTAMP)")
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("event_discovery_runs")}
        assert {"trigger_kind", "dedupe_key"}.issubset(columns)
        indexes = {item["name"] for item in inspector.get_indexes("event_discovery_runs")}
        assert "uq_event_discovery_runs_dedupe_key" in indexes
        row = connection.execute(
            text("SELECT id, status, trigger_kind FROM event_discovery_runs WHERE id='legacy-run'")
        ).one()
        assert row.id == "legacy-run"
        assert row.status == "completed"
        assert row.trigger_kind == "analyst"
    engine.dispose()


def test_0004_migration_adds_activity_table_to_existing_0003_schema():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    migration = importlib.import_module("migrations.versions.0004_incident_activity_timeline")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE incident_activities")
        assert not inspect(connection).has_table("incident_activities")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        inspector = inspect(connection)
        assert inspector.has_table("incident_activities")
        columns = {column["name"] for column in inspector.get_columns("incident_activities")}
        assert {
            "id", "incident_id", "actor_user_id", "actor_display_name", "actor_role",
            "activity_type", "message", "location_text", "latitude", "longitude", "created_at",
        }.issubset(columns)
        indexes = {index["name"] for index in inspector.get_indexes("incident_activities")}
        assert "ix_incident_activity_timeline" in indexes
    engine.dispose()


def test_0005_migration_adds_ai_analysis_to_existing_0004_schema():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    migration = importlib.import_module("migrations.versions.0005_multimodal_ai_pipeline")
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE reports DROP COLUMN ai_analysis")
        assert "ai_analysis" not in {item["name"] for item in inspect(connection).get_columns("reports")}
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        columns = {item["name"] for item in inspect(connection).get_columns("reports")}
        assert "ai_analysis" in columns
        with Operations.context(context):
            migration.upgrade()  # must be safe on an already-upgraded schema
        columns = {item["name"] for item in inspect(connection).get_columns("reports")}
        assert "ai_analysis" in columns
    engine.dispose()


def test_0006_expands_report_location_text_and_preserves_existing_values():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    long_location = "Flood-affected settlement, administrative block " * 12
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE reports (id VARCHAR(36) PRIMARY KEY, location_text VARCHAR(220))"
        )
        connection.execute(
            text("INSERT INTO reports (id, location_text) VALUES (:id, :location)"),
            {"id": "report-long-location", "location": long_location},
        )
        migration = importlib.import_module(
            "migrations.versions.0006_expand_report_location_text"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        column = next(
            item for item in inspect(connection).get_columns("reports")
            if item["name"] == "location_text"
        )
        assert column["type"].__class__.__name__.upper() == "TEXT"
        assert connection.execute(
            text("SELECT location_text FROM reports WHERE id = :id"),
            {"id": "report-long-location"},
        ).scalar_one() == long_location
        with Operations.context(context):
            with pytest.raises(RuntimeError, match="values exceed 220 characters"):
                migration.downgrade()
    engine.dispose()


def test_0007_migration_creates_connector_run_telemetry_table_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migration = importlib.import_module("migrations.versions.0007_integration_run_telemetry")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert inspector.has_table("integration_runs")
        columns = {item["name"] for item in inspector.get_columns("integration_runs")}
        assert {
            "id", "source_type", "source_name", "trigger", "status", "records_fetched",
            "records_inserted", "records_duplicate", "records_rejected", "records_queued",
            "queue_failures", "rejection_counts", "failure_code", "retryable", "started_at",
            "completed_at", "duration_ms",
        }.issubset(columns)
        indexes = {item["name"] for item in inspector.get_indexes("integration_runs")}
        assert "ix_integration_runs_source_started" in indexes
        assert "ix_integration_runs_status_started" in indexes
    engine.dispose()


def test_0008_migration_adds_source_observed_at_without_changing_existing_report_times():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE reports (id VARCHAR(36) PRIMARY KEY, created_at DATETIME)")
        connection.execute(
            text("INSERT INTO reports (id, created_at) VALUES ('existing', '2026-01-01 00:00:00')")
        )
        migration = importlib.import_module("migrations.versions.0008_report_source_observed_at")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        columns = {item["name"] for item in inspect(connection).get_columns("reports")}
        assert "source_observed_at" in columns
        assert connection.execute(text("SELECT created_at FROM reports WHERE id='existing'")).scalar_one() == "2026-01-01 00:00:00"
        indexes = {item["name"] for item in inspect(connection).get_indexes("reports")}
        assert "ix_reports_source_observed_at" in indexes
    engine.dispose()


def test_0009_migration_tracks_analysis_generations_and_connector_attempts():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE reports (id VARCHAR(36) PRIMARY KEY, created_at DATETIME, ai_analysis JSON, ai_category VARCHAR(32))"
        )
        connection.exec_driver_sql("CREATE TABLE integration_runs (id VARCHAR(36) PRIMARY KEY)")
        connection.execute(
            text("INSERT INTO reports (id, ai_analysis, ai_category) VALUES (:id, :analysis, :category)"),
            {"id": "analyzed", "analysis": '{"existing": true}', "category": "flood"},
        )
        connection.execute(
            text("INSERT INTO reports (id, ai_analysis, ai_category) VALUES (:id, NULL, NULL)"),
            {"id": "pending"},
        )
        migration = importlib.import_module(
            "migrations.versions.0009_analysis_generations_and_retry_telemetry"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()  # Idempotent when the release verifier is rerun.
        reports = connection.execute(text(
            "SELECT id, analysis_revision, analyzed_revision FROM reports ORDER BY id"
        )).all()
        assert reports == [("analyzed", 1, 1), ("pending", 1, 0)]
        run_columns = {item["name"] for item in inspect(connection).get_columns("integration_runs")}
        assert "fetch_attempts" in run_columns
        assert connection.execute(
            text("SELECT fetch_attempts FROM integration_runs")
        ).first() is None
    engine.dispose()


def test_0010_migration_creates_event_hypothesis_schema_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE event_discovery_runs")
        connection.exec_driver_sql("DROP TABLE event_hypothesis_reports")
        connection.exec_driver_sql("DROP TABLE event_hypotheses")
        migration = importlib.import_module("migrations.versions.0010_event_hypotheses")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert {
            "event_hypotheses", "event_hypothesis_reports", "event_discovery_runs",
        }.issubset(set(inspector.get_table_names()))
        hypothesis_indexes = {item["name"] for item in inspector.get_indexes("event_hypotheses")}
        assert "ix_event_hypotheses_status_last_observed" in hypothesis_indexes
        association_indexes = {
            item["name"] for item in inspector.get_indexes("event_hypothesis_reports")
        }
        assert "ix_event_hypothesis_reports_review" in association_indexes
    engine.dispose()



def test_0011_migration_adds_semantic_temporal_and_source_profile_tables_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE source_reliability_profiles")
        connection.exec_driver_sql("DROP TABLE event_hypothesis_snapshots")
        connection.exec_driver_sql("DROP TABLE report_semantic_embeddings")
        connection.exec_driver_sql("ALTER TABLE event_discovery_runs DROP COLUMN result_summary")
        migration = importlib.import_module(
            "migrations.versions.0011_semantic_temporal_source_intelligence"
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert {
            "report_semantic_embeddings",
            "event_hypothesis_snapshots",
            "source_reliability_profiles",
        }.issubset(set(inspector.get_table_names()))
        run_columns = {
            item["name"] for item in inspector.get_columns("event_discovery_runs")
        }
        assert "result_summary" in run_columns
        snapshot_indexes = {
            item["name"] for item in inspector.get_indexes("event_hypothesis_snapshots")
        }
        assert "ix_event_hypothesis_snapshots_timeline" in snapshot_indexes
        embedding_indexes = {
            item["name"] for item in inspector.get_indexes("report_semantic_embeddings")
        }
        assert "ix_report_semantic_embeddings_model_hash" in embedding_indexes
    engine.dispose()


def test_0012_migration_creates_research_dataset_governance_schema_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE research_dataset_version_items")
        connection.exec_driver_sql("DROP TABLE research_dataset_versions")
        connection.exec_driver_sql("DROP TABLE event_pair_labels")
        connection.exec_driver_sql("DROP TABLE research_datasets")
        migration = importlib.import_module("migrations.versions.0012_research_data_engine")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert {
            "research_datasets", "event_pair_labels",
            "research_dataset_versions", "research_dataset_version_items",
        }.issubset(set(inspector.get_table_names()))
        label_columns = {item["name"] for item in inspector.get_columns("event_pair_labels")}
        assert {
            "dataset_id", "report_a_id", "report_b_id", "pair_fingerprint",
            "label", "split_group", "review_status", "privacy_review_status",
            "provenance_snapshot", "reviewed_by",
        }.issubset(label_columns)
        dataset_columns = {item["name"] for item in inspector.get_columns("research_datasets")}
        assert "coordinate_precision" in dataset_columns
        version_columns = {item["name"] for item in inspector.get_columns("research_dataset_versions")}
        assert {"content_sha256", "record_count", "manifest", "sealed_at"}.issubset(version_columns)
        indexes = {item["name"] for item in inspector.get_indexes("event_pair_labels")}
        assert "ix_event_pair_labels_dataset_review" in indexes
        item_indexes = {
            item["name"] for item in inspector.get_indexes("research_dataset_version_items")
        }
        assert "ix_research_dataset_version_items_group" in item_indexes
    engine.dispose()


def test_0015_migration_adds_archive_backfill_active_learning_and_evaluation_tables_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    tables = (
        "model_evaluation_runs", "event_pair_candidates", "pair_candidate_mining_runs",
        "acquisition_backfill_items", "acquisition_backfill_runs", "acquisition_observations",
    )
    with engine.begin() as connection:
        for table in tables:
            connection.exec_driver_sql(f'DROP TABLE "{table}"')
        migration = importlib.import_module("migrations.versions.0015_acq_archive_learning")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        for table in tables:
            assert inspector.has_table(table)
        observation_columns = {item["name"] for item in inspector.get_columns("acquisition_observations")}
        assert {"source_type", "source_record_id", "content_sha256", "normalized_payload", "seen_count"}.issubset(observation_columns)
        backfill_columns = {item["name"] for item in inspector.get_columns("acquisition_backfill_runs")}
        assert {"cursor_at", "page_offset", "lease_token", "lease_expires_at", "max_records"}.issubset(backfill_columns)
        item_columns = {item["name"] for item in inspector.get_columns("acquisition_backfill_items")}
        assert {"backfill_run_id", "observation_id", "report_id", "analysis_status", "queue_attempts"}.issubset(item_columns)
        mining_columns = {item["name"] for item in inspector.get_columns("pair_candidate_mining_runs")}
        assert {"attempts", "lease_token", "lease_expires_at", "started_at", "updated_at"}.issubset(mining_columns)
        candidate_columns = {item["name"] for item in inspector.get_columns("event_pair_candidates")}
        assert {"pair_fingerprint", "heuristic_score", "selection_score", "status"}.issubset(candidate_columns)
    engine.dispose()


def test_0017_migration_adds_response_plan_orchestration_idempotently():
    import importlib
    from datetime import UTC, datetime

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError

    engine = create_engine("sqlite:///:memory:")
    migration = importlib.import_module("migrations.versions.0017_response_plan_orchestration")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE incidents (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE incident_actions (id VARCHAR(36) PRIMARY KEY, incident_id VARCHAR(36) NOT NULL "
            "REFERENCES incidents(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE incident_action_events (id VARCHAR(36) PRIMARY KEY, action_id VARCHAR(36) NOT NULL "
            "REFERENCES incident_actions(id) ON DELETE CASCADE)"
        )
        connection.exec_driver_sql("INSERT INTO incidents (id) VALUES ('incident-legacy')")
        connection.exec_driver_sql("INSERT INTO incident_actions (id, incident_id) VALUES ('legacy-action', 'incident-legacy')")
        connection.exec_driver_sql("INSERT INTO incident_action_events (id, action_id) VALUES ('legacy-event', 'legacy-action')")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()

        inspector = inspect(connection)
        assert {"incident_response_plans", "incident_action_dependencies"}.issubset(
            set(inspector.get_table_names())
        )
        action_columns = {item["name"] for item in inspector.get_columns("incident_actions")}
        assert {"response_plan_id", "sequence_order"}.issubset(action_columns)
        legacy_action = connection.execute(text(
            "SELECT response_plan_id, sequence_order FROM incident_actions WHERE id = 'legacy-action'"
        )).one()
        assert legacy_action == (None, 0)
        retained_event_count = connection.execute(text(
            "SELECT count(*) FROM incident_action_events WHERE id = 'legacy-event'"
        )).scalar_one()
        assert retained_event_count == 1
        plan_indexes = {item["name"] for item in inspector.get_indexes("incident_response_plans")}
        assert "uq_incident_response_plans_active_incident" in plan_indexes
        dependency_constraints = {
            item.get("name") for item in inspector.get_unique_constraints("incident_action_dependencies")
        }
        assert "uq_incident_action_dependencies_unique" in dependency_constraints

        connection.execute(text("INSERT INTO incidents (id) VALUES ('incident-1')"))
        connection.execute(text("INSERT INTO users (id) VALUES ('user-1')"))
        plan_values = {
            "id": "plan-1", "incident_id": "incident-1", "template_key": "flood_coordination_v1",
            "template_version": 1, "title": "Flood plan", "status": "active",
            "idempotency_key": "request-key-00000001", "template_snapshot": "{}",
            "created_by": "user-1", "created_at": now, "updated_at": now,
        }
        connection.execute(text("""INSERT INTO incident_response_plans
            (id, incident_id, template_key, template_version, title, status, idempotency_key,
             template_snapshot, created_by, created_at, updated_at)
            VALUES (:id, :incident_id, :template_key, :template_version, :title, :status,
                    :idempotency_key, :template_snapshot, :created_by, :created_at, :updated_at)"""),
            plan_values,
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO incident_response_plans
                    (id, incident_id, template_key, template_version, title, status, idempotency_key,
                     template_snapshot, created_by, created_at, updated_at)
                    VALUES ('plan-2', 'incident-1', 'flood_coordination_v1', 1, 'Another plan', 'active',
                            'request-key-00000002', '{}', 'user-1', :created_at, :updated_at)"""),
                    {"created_at": now, "updated_at": now},
                )
        connection.execute(text("""INSERT INTO incident_actions
            (id, incident_id, response_plan_id, sequence_order)
            VALUES ('action-1', 'incident-1', 'plan-1', 0), ('action-2', 'incident-1', 'plan-1', 1)"""))
        connection.execute(text("""INSERT INTO incident_action_dependencies
            (id, plan_id, action_id, depends_on_action_id, created_at)
            VALUES ('edge-1', 'plan-1', 'action-2', 'action-1', :created_at)"""), {"created_at": now})
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""INSERT INTO incident_action_dependencies
                    (id, plan_id, action_id, depends_on_action_id, created_at)
                    VALUES ('edge-2', 'plan-1', 'action-2', 'action-1', :created_at)"""), {"created_at": now})
    engine.dispose()



def test_0018_migration_creates_durable_job_outbox_idempotently():
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    migration = importlib.import_module("migrations.versions.0018_durable_job_outbox")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()
        inspector = inspect(connection)
        assert inspector.has_table("background_job_outbox")
        columns = {item["name"] for item in inspector.get_columns("background_job_outbox")}
        assert {
            "id", "job_id", "function_path", "task_args", "queue_name",
            "job_timeout_seconds", "result_ttl_seconds", "status", "attempts",
            "available_at", "lease_token", "lease_expires_at", "enqueued_at",
            "last_error_code", "created_at", "updated_at",
        }.issubset(columns)
        indexes = {item["name"] for item in inspector.get_indexes("background_job_outbox")}
        assert {
            "ix_background_job_outbox_status_available",
            "ix_background_job_outbox_lease",
        }.issubset(indexes)
        constraints = {
            item.get("name") for item in inspector.get_unique_constraints("background_job_outbox")
        }
        assert "uq_background_job_outbox_job_id" in constraints
        connection.execute(text("""INSERT INTO background_job_outbox
            (id, job_id, function_path, task_args, queue_name, job_timeout_seconds,
             result_ttl_seconds, status, attempts, available_at, created_at, updated_at)
            VALUES ('outbox-1', 'cignal-report-analysis-r1', 'app.worker.process_report',
                    '[\"report-1\",1]', 'cignal', 300, 3600, 'pending', 0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""))
        with Operations.context(context):
            migration.upgrade()
        assert connection.execute(text(
            "SELECT job_id, status FROM background_job_outbox WHERE id='outbox-1'"
        )).one() == ("cignal-report-analysis-r1", "pending")
    engine.dispose()
