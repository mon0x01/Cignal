"""Add transactional background-job outbox.

Revision ID: 0018_durable_job_outbox
Revises: 0017_response_plan_orchestration
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_durable_job_outbox"
down_revision = "0017_response_plan_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("background_job_outbox"):
        op.create_table(
            "background_job_outbox",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_id", sa.String(180), nullable=False),
            sa.Column("function_path", sa.String(180), nullable=False),
            sa.Column("task_args", sa.JSON(), nullable=False),
            sa.Column("queue_name", sa.String(80), nullable=False, server_default="cignal"),
            sa.Column("job_timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
            sa.Column("result_ttl_seconds", sa.Integer(), nullable=False, server_default="3600"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_token", sa.String(36), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(80), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", name="uq_background_job_outbox_job_id"),
            sa.CheckConstraint(
                "status IN ('pending','publishing','enqueued','dead')",
                name="ck_background_job_outbox_status",
            ),
            sa.CheckConstraint("attempts >= 0", name="ck_background_job_outbox_attempts"),
            sa.CheckConstraint("job_timeout_seconds >= 1", name="ck_background_job_outbox_timeout"),
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("background_job_outbox")}
    for name, columns in (
        ("ix_background_job_outbox_status_available", ["status", "available_at"]),
        ("ix_background_job_outbox_lease", ["status", "lease_expires_at"]),
        ("ix_background_job_outbox_status", ["status"]),
        ("ix_background_job_outbox_available_at", ["available_at"]),
        ("ix_background_job_outbox_lease_token", ["lease_token"]),
        ("ix_background_job_outbox_lease_expires_at", ["lease_expires_at"]),
        ("ix_background_job_outbox_enqueued_at", ["enqueued_at"]),
    ):
        if name not in indexes:
            op.create_index(name, "background_job_outbox", columns)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("background_job_outbox"):
        return
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("background_job_outbox")}
    for name in (
        "ix_background_job_outbox_enqueued_at",
        "ix_background_job_outbox_lease_expires_at",
        "ix_background_job_outbox_lease_token",
        "ix_background_job_outbox_available_at",
        "ix_background_job_outbox_status",
        "ix_background_job_outbox_lease",
        "ix_background_job_outbox_status_available",
    ):
        if name in indexes:
            op.drop_index(name, table_name="background_job_outbox")
    op.drop_table("background_job_outbox")
