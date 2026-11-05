"""Track connector ingestion runs, source health and record-level outcomes.

Revision ID: 0007_integration_run_telemetry
Revises: 0006_expand_report_location_text
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_integration_run_telemetry"
down_revision = "0006_expand_report_location_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "integration_runs" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "integration_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="scheduled"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_queued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queue_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejection_counts", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_integration_runs_source_type", "integration_runs", ["source_type"])
    op.create_index("ix_integration_runs_status", "integration_runs", ["status"])
    op.create_index("ix_integration_runs_started_at", "integration_runs", ["started_at"])
    op.create_index(
        "ix_integration_runs_source_started", "integration_runs", ["source_type", "started_at"]
    )
    op.create_index(
        "ix_integration_runs_status_started", "integration_runs", ["status", "started_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "integration_runs" not in sa.inspect(bind).get_table_names():
        return
    op.drop_index("ix_integration_runs_status_started", table_name="integration_runs")
    op.drop_index("ix_integration_runs_source_started", table_name="integration_runs")
    op.drop_index("ix_integration_runs_started_at", table_name="integration_runs")
    op.drop_index("ix_integration_runs_status", table_name="integration_runs")
    op.drop_index("ix_integration_runs_source_type", table_name="integration_runs")
    op.drop_table("integration_runs")
