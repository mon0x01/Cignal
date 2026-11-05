"""Track report analysis generations and connector fetch attempts.

Revision ID: 0009_analysis_retry_v1
Revises: 0008_report_source_observed_at
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_analysis_retry_v1"
down_revision = "0008_report_source_observed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    report_columns = {item["name"] for item in sa.inspect(bind).get_columns("reports")}
    if "analysis_revision" not in report_columns:
        op.add_column(
            "reports",
            sa.Column("analysis_revision", sa.Integer(), nullable=False, server_default="1"),
        )
    if "analyzed_revision" not in report_columns:
        op.add_column(
            "reports",
            sa.Column("analyzed_revision", sa.Integer(), nullable=False, server_default="0"),
        )

    # Existing analyzed reports are considered current at generation 1. Pending reports
    # retain analyzed_revision=0 so the worker backlog sweep can recover them.
    bind.execute(sa.text(
        "UPDATE reports SET analyzed_revision = 1 "
        "WHERE analysis_revision = 1 AND analyzed_revision = 0 "
        "AND (ai_analysis IS NOT NULL OR ai_category IS NOT NULL)"
    ))
    report_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("reports")}
    if "ix_reports_analysis_pending" not in report_indexes:
        op.create_index(
            "ix_reports_analysis_pending", "reports", ["created_at", "id"],
            postgresql_where=sa.text("analysis_revision > analyzed_revision"),
            sqlite_where=sa.text("analysis_revision > analyzed_revision"),
        )

    run_columns = {item["name"] for item in sa.inspect(bind).get_columns("integration_runs")}
    if "fetch_attempts" not in run_columns:
        op.add_column(
            "integration_runs",
            sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    run_columns = {item["name"] for item in sa.inspect(bind).get_columns("integration_runs")}
    if "fetch_attempts" in run_columns:
        op.drop_column("integration_runs", "fetch_attempts")
    report_columns = {item["name"] for item in sa.inspect(bind).get_columns("reports")}
    report_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("reports")}
    if "ix_reports_analysis_pending" in report_indexes:
        op.drop_index("ix_reports_analysis_pending", table_name="reports")
    if "analyzed_revision" in report_columns:
        op.drop_column("reports", "analyzed_revision")
    if "analysis_revision" in report_columns:
        op.drop_column("reports", "analysis_revision")
