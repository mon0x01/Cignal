"""Support coalesced continuous cross-source event discovery.

Revision ID: 0019_continuous_event_discovery
Revises: 0018_durable_job_outbox
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_continuous_event_discovery"
down_revision = "0018_durable_job_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("event_discovery_runs")}
    if "trigger_kind" not in columns:
        op.add_column(
            "event_discovery_runs",
            sa.Column("trigger_kind", sa.String(16), nullable=False, server_default="analyst"),
        )
    if "dedupe_key" not in columns:
        op.add_column("event_discovery_runs", sa.Column("dedupe_key", sa.String(100), nullable=True))
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_discovery_runs")}
    if "uq_event_discovery_runs_dedupe_key" not in indexes:
        op.create_index(
            "uq_event_discovery_runs_dedupe_key",
            "event_discovery_runs",
            ["dedupe_key"],
            unique=True,
        )
    if sa.inspect(bind).has_table("reports"):
        report_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("reports")}
        if "ix_reports_updated_at" not in report_indexes:
            op.create_index("ix_reports_updated_at", "reports", ["updated_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("event_discovery_runs"):
        return
    if sa.inspect(bind).has_table("reports"):
        report_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("reports")}
        if "ix_reports_updated_at" in report_indexes:
            op.drop_index("ix_reports_updated_at", table_name="reports")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_discovery_runs")}
    if "uq_event_discovery_runs_dedupe_key" in indexes:
        op.drop_index("uq_event_discovery_runs_dedupe_key", table_name="event_discovery_runs")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("event_discovery_runs")}
    for name in ("dedupe_key", "trigger_kind"):
        if name in columns:
            op.drop_column("event_discovery_runs", name)
