"""Preserve the provider's event-observation time for temporal correlation.

Revision ID: 0008_report_source_observed_at
Revises: 0007_integration_run_telemetry
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_report_source_observed_at"
down_revision = "0007_integration_run_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("reports")}
    if "source_observed_at" not in columns:
        op.add_column("reports", sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_reports_source_observed_at", "reports", ["source_observed_at"])


def downgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("reports")}
    if "source_observed_at" in columns:
        op.drop_index("ix_reports_source_observed_at", table_name="reports")
        op.drop_column("reports", "source_observed_at")
