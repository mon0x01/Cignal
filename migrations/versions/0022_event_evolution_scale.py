"""Index report-to-hypothesis lookups used by deterministic event replay.

Revision ID: 0022_event_evolution_scale
Revises: 0021_job_execution_reliability
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_event_evolution_scale"
down_revision = "0021_job_execution_reliability"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_event_hypothesis_reports_report_status_hypothesis"
_INDEX_COLUMNS = ["report_id", "association_status", "hypothesis_id"]


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("event_hypothesis_reports"):
        raise RuntimeError("event_hypothesis_reports must exist before event evolution scale migration")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_hypothesis_reports")}
    if _INDEX_NAME not in indexes:
        op.create_index(_INDEX_NAME, "event_hypothesis_reports", _INDEX_COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("event_hypothesis_reports"):
        return
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_hypothesis_reports")}
    if _INDEX_NAME in indexes:
        op.drop_index(_INDEX_NAME, table_name="event_hypothesis_reports")
