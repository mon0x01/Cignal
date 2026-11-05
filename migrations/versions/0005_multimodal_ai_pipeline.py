"""Persist versioned multimodal AI analysis payloads.

Revision ID: 0005_multimodal_ai_pipeline
Revises: 0004_incident_activity_timeline
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_multimodal_ai_pipeline"
down_revision = "0004_incident_activity_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("reports")}
    if "ai_analysis" not in columns:
        op.add_column("reports", sa.Column("ai_analysis", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("reports")}
    if "ai_analysis" in columns:
        op.drop_column("reports", "ai_analysis")
