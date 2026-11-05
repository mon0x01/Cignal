"""Persist source governance and provider provenance on ingested reports.

Revision ID: 0013_source_provenance
Revises: 0012_research_data_v1
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_source_provenance"
down_revision = "0012_research_data_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("reports"):
        raise RuntimeError("Cannot add source provenance: reports table is missing")
    columns = {column["name"] for column in inspector.get_columns("reports")}
    if "source_metadata" not in columns:
        op.add_column(
            "reports",
            sa.Column(
                "source_metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("reports"):
        columns = {column["name"] for column in inspector.get_columns("reports")}
        if "source_metadata" in columns:
            op.drop_column("reports", "source_metadata")
