"""Preserve full normalized location descriptions on reports.

Revision ID: 0006_expand_report_location_text
Revises: 0005_multimodal_ai_pipeline
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_expand_report_location_text"
down_revision = "0005_multimodal_ai_pipeline"
branch_labels = None
depends_on = None


def _column_type(bind) -> str | None:
    if bind.dialect.name == "postgresql":
        return bind.execute(sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'reports' "
            "AND column_name = 'location_text'"
        )).scalar_one_or_none()
    if bind.dialect.name == "sqlite":
        columns = bind.exec_driver_sql("PRAGMA table_info('reports')").fetchall()
        return next((str(column[2]).upper() for column in columns if column[1] == "location_text"), None)
    columns = sa.inspect(bind).get_columns("reports")
    column = next((item for item in columns if item["name"] == "location_text"), None)
    return type(column["type"]).__name__.upper() if column else None


def upgrade() -> None:
    bind = op.get_bind()
    current_type = _column_type(bind)
    if current_type is None:
        raise RuntimeError("reports.location_text is missing; refusing an incomplete migration")
    if current_type.lower() not in {"text", "clob"}:
        with op.batch_alter_table("reports") as batch:
            batch.alter_column(
                "location_text",
                existing_type=sa.String(length=220),
                type_=sa.Text(),
                existing_nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT COALESCE(MAX(length(location_text)), 0) FROM reports"
    )).scalar_one() > 220:
        raise RuntimeError(
            "Cannot downgrade reports.location_text: values exceed 220 characters. "
            "Shorten or archive them before retrying."
        )
    with op.batch_alter_table("reports") as batch:
        batch.alter_column(
            "location_text",
            existing_type=sa.Text(),
            type_=sa.String(length=220),
            existing_nullable=True,
        )
