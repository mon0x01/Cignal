"""Add semantic embedding cache, event snapshots and source evidence profiles.

Revision ID: 0011_event_intel_v2
Revises: 0010_event_hypotheses
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_event_intel_v2"
down_revision = "0010_event_hypotheses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("event_discovery_runs"):
        run_columns = {item["name"] for item in inspector.get_columns("event_discovery_runs")}
        if "result_summary" not in run_columns:
            op.add_column(
                "event_discovery_runs",
                sa.Column("result_summary", sa.JSON(), nullable=False, server_default="{}"),
            )

    if not inspector.has_table("report_semantic_embeddings"):
        op.create_table(
            "report_semantic_embeddings",
            sa.Column("report_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("model_version", sa.String(100), primary_key=True),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("dimensions", sa.Integer(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("event_hypothesis_snapshots"):
        op.create_table(
            "event_hypothesis_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("hypothesis_id", sa.String(36), sa.ForeignKey("event_hypotheses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("discovery_run_id", sa.String(36), sa.ForeignKey("event_discovery_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("snapshot_key", sa.String(64), nullable=False),
            sa.Column("change_kind", sa.String(20), nullable=False),
            sa.Column("report_count", sa.Integer(), nullable=False),
            sa.Column("distinct_source_count", sa.Integer(), nullable=False),
            sa.Column("match_score", sa.Float(), nullable=False),
            sa.Column("centroid_latitude", sa.Float(), nullable=False),
            sa.Column("centroid_longitude", sa.Float(), nullable=False),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("snapshot_data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("hypothesis_id", "snapshot_key", name="uq_event_hypothesis_snapshot_key"),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("source_reliability_profiles"):
        op.create_table(
            "source_reliability_profiles",
            sa.Column("source_origin", sa.String(150), primary_key=True),
            sa.Column("source_type", sa.String(24), nullable=False),
            sa.Column("source_name", sa.String(100), nullable=False),
            sa.Column("ingestion_runs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("successful_runs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("partial_runs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_runs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("records_inserted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reviewed_associations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("confirmed_associations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rejected_associations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("operational_success_rate", sa.Float(), nullable=True),
            sa.Column("review_acceptance_posterior_mean", sa.Float(), nullable=True),
            sa.Column("review_acceptance_wilson_lower", sa.Float(), nullable=True),
            sa.Column("review_confidence", sa.String(20), nullable=False, server_default="insufficient"),
            sa.Column("method_version", sa.String(40), nullable=False),
            sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # Index creation is guarded so this migration is safe to rerun in SQLite tests
    # and in recovery scenarios where tables were created before an interrupted run.
    for table, index_name, columns in (
        ("report_semantic_embeddings", "ix_report_semantic_embeddings_model_hash", ["model_version", "content_sha256"]),
        ("event_hypothesis_snapshots", "ix_event_hypothesis_snapshots_timeline", ["hypothesis_id", "created_at"]),
        ("event_hypothesis_snapshots", "ix_event_hypothesis_snapshots_created_at", ["created_at"]),
        ("source_reliability_profiles", "ix_source_reliability_profiles_source_type", ["source_type"]),
        ("source_reliability_profiles", "ix_source_reliability_type_updated", ["source_type", "updated_at"]),
    ):
        existing = {item["name"] for item in sa.inspect(bind).get_indexes(table)}
        if index_name not in existing:
            op.create_index(index_name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "source_reliability_profiles",
        "event_hypothesis_snapshots",
        "report_semantic_embeddings",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
    if inspector.has_table("event_discovery_runs"):
        columns = {item["name"] for item in inspector.get_columns("event_discovery_runs")}
        if "result_summary" in columns:
            op.drop_column("event_discovery_runs", "result_summary")
