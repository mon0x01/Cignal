"""Add immutable acquisition observations, resumable backfills and active-learning candidates.

Revision ID: 0015_acq_archive_learning
Revises: 0014_acq_source_sched
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_acq_archive_learning"
down_revision = "0014_acq_source_sched"
branch_labels = None
depends_on = None


def _create_table_if_missing(name: str, *columns, constraints=()):
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(name):
        op.create_table(name, *columns, *constraints)


def _index_if_missing(name: str, table: str, columns: list[str], *, unique: bool = False):
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    _create_table_if_missing(
        "acquisition_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_record_id", sa.String(180), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(700), nullable=True),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("replay_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_replayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        constraints=(sa.UniqueConstraint(
            "source_type", "source_record_id", "content_sha256",
            name="uq_acq_observation_version",
        ),),
    )
    _index_if_missing("ix_acq_observations_report_id", "acquisition_observations", ["report_id"])
    _index_if_missing("ix_acq_observations_source_seen", "acquisition_observations", ["source_type", "last_seen_at"])
    _index_if_missing("ix_acq_observations_observed", "acquisition_observations", ["observed_at"])

    _create_table_if_missing(
        "acquisition_backfill_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("page_offset", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_records", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observations_captured", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_queued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("analysis_queue_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("windows_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index_if_missing("ix_acquisition_backfill_runs_source_type", "acquisition_backfill_runs", ["source_type"])
    _index_if_missing("ix_acquisition_backfill_runs_lease_token", "acquisition_backfill_runs", ["lease_token"])
    _index_if_missing("ix_acquisition_backfill_runs_lease_expires_at", "acquisition_backfill_runs", ["lease_expires_at"])
    _index_if_missing("ix_acq_backfill_status_created", "acquisition_backfill_runs", ["status", "created_at"])
    _index_if_missing("ix_acq_backfill_source_cursor", "acquisition_backfill_runs", ["source_type", "cursor_at"])

    _create_table_if_missing(
        "acquisition_backfill_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("backfill_run_id", sa.String(36), sa.ForeignKey("acquisition_backfill_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.String(36), sa.ForeignKey("acquisition_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("analysis_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("queue_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        constraints=(sa.UniqueConstraint(
            "backfill_run_id", "observation_id", name="uq_acq_backfill_item_observation",
        ),),
    )
    _index_if_missing(
        "ix_acq_backfill_items_run_status", "acquisition_backfill_items",
        ["backfill_run_id", "analysis_status"],
    )

    _create_table_if_missing(
        "pair_candidate_mining_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("reports_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pairs_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_existing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index_if_missing("ix_pair_candidate_mining_runs_dataset_id", "pair_candidate_mining_runs", ["dataset_id"])
    _index_if_missing("ix_pair_candidate_mining_runs_status", "pair_candidate_mining_runs", ["status"])
    _index_if_missing("ix_pair_candidate_mining_runs_lease_token", "pair_candidate_mining_runs", ["lease_token"])
    _index_if_missing("ix_pair_candidate_mining_runs_lease_expires_at", "pair_candidate_mining_runs", ["lease_expires_at"])

    _create_table_if_missing(
        "event_pair_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mining_run_id", sa.String(36), sa.ForeignKey("pair_candidate_mining_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("report_a_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("report_b_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pair_fingerprint", sa.String(64), nullable=False),
        sa.Column("heuristic_score", sa.Float(), nullable=False),
        sa.Column("supervised_score", sa.Float(), nullable=True),
        sa.Column("selection_score", sa.Float(), nullable=False),
        sa.Column("model_version", sa.String(100), nullable=True),
        sa.Column("candidate_reasons", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        constraints=(sa.UniqueConstraint(
            "dataset_id", "pair_fingerprint", name="uq_event_pair_candidate_fingerprint",
        ),),
    )
    _index_if_missing("ix_event_pair_candidates_dataset_id", "event_pair_candidates", ["dataset_id"])
    _index_if_missing("ix_event_pair_candidates_mining_run_id", "event_pair_candidates", ["mining_run_id"])
    _index_if_missing("ix_event_pair_candidates_report_a_id", "event_pair_candidates", ["report_a_id"])
    _index_if_missing("ix_event_pair_candidates_report_b_id", "event_pair_candidates", ["report_b_id"])
    _index_if_missing("ix_event_pair_candidates_status", "event_pair_candidates", ["status"])
    _index_if_missing("ix_event_pair_candidates_review", "event_pair_candidates", ["dataset_id", "status", "selection_score"])

    _create_table_if_missing(
        "model_evaluation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_type", sa.String(48), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("model_version", sa.String(120), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("group_count", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("baseline_metrics", sa.JSON(), nullable=False),
        sa.Column("comparison", sa.JSON(), nullable=False),
        sa.Column("evaluation_policy", sa.String(80), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    _index_if_missing("ix_model_evaluation_runs_task_type", "model_evaluation_runs", ["task_type"])
    _index_if_missing("ix_model_evaluation_runs_dataset_sha256", "model_evaluation_runs", ["dataset_sha256"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "model_evaluation_runs",
        "event_pair_candidates",
        "acquisition_backfill_items",
        "pair_candidate_mining_runs",
        "acquisition_backfill_runs",
        "acquisition_observations",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
