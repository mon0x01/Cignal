"""Add execution leases, attempt history and queue-lane dispatch.

Revision ID: 0021_job_execution_reliability
Revises: 0020_event_evolution_replay
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_job_execution_reliability"
down_revision = "0020_event_evolution_replay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("background_job_outbox"):
        raise RuntimeError("background_job_outbox must exist before execution reliability migration")

    existing_columns = {item["name"] for item in inspector.get_columns("background_job_outbox")}
    existing_checks = {
        item.get("name") for item in inspector.get_check_constraints("background_job_outbox")
    }
    with op.batch_alter_table("background_job_outbox") as batch:
        if "ck_background_job_outbox_status" in existing_checks:
            batch.drop_constraint("ck_background_job_outbox_status", type_="check")
        batch.create_check_constraint(
            "ck_background_job_outbox_status",
            "status IN ('pending','publishing','enqueued','running','completed','dead')",
        )
        columns = (
            sa.Column("last_rq_job_id", sa.String(80), nullable=True),
            sa.Column("execution_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("execution_lease_token", sa.String(36), nullable=True),
            sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_execution_error_code", sa.String(80), nullable=True),
        )
        for column in columns:
            if column.name not in existing_columns:
                batch.add_column(column)
        current_checks = existing_checks - {"ck_background_job_outbox_status"}
        if "ck_background_job_outbox_execution_attempts" not in current_checks:
            batch.create_check_constraint(
                "ck_background_job_outbox_execution_attempts", "execution_attempts >= 0"
            )
        if "ck_background_job_outbox_execution_generation" not in current_checks:
            batch.create_check_constraint(
                "ck_background_job_outbox_execution_generation", "execution_generation >= 0"
            )

    # Preserve pointers to legacy RQ jobs that were dispatched before the wrapper existed.
    op.execute(
        sa.text(
            "UPDATE background_job_outbox SET last_rq_job_id = job_id "
            "WHERE status IN ('enqueued','publishing') AND last_rq_job_id IS NULL"
        )
    )

    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("background_job_outbox")}
    for name, columns in (
        ("ix_background_job_outbox_last_rq_job_id", ["last_rq_job_id"]),
        ("ix_background_job_outbox_execution_lease_token", ["execution_lease_token"]),
        ("ix_background_job_outbox_execution_lease", ["status", "execution_lease_expires_at"]),
    ):
        if name not in indexes:
            op.create_index(name, "background_job_outbox", columns)

    if not sa.inspect(bind).has_table("background_job_execution_attempts"):
        op.create_table(
            "background_job_execution_attempts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "outbox_id",
                sa.String(36),
                sa.ForeignKey("background_job_outbox.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("rq_job_id", sa.String(80), nullable=False),
            sa.Column("worker_name", sa.String(160), nullable=False),
            sa.Column("queue_name", sa.String(80), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_code", sa.String(80), nullable=True),
            sa.Column("result_sha256", sa.String(64), nullable=True),
            sa.UniqueConstraint(
                "outbox_id", "generation", "attempt_number",
                name="uq_background_job_execution_generation_attempt",
            ),
            sa.CheckConstraint(
                "generation >= 0 AND attempt_number >= 1",
                name="ck_background_job_execution_attempt_number",
            ),
            sa.CheckConstraint(
                "status IN ('running','completed','failed','abandoned')",
                name="ck_background_job_execution_status",
            ),
        )

    attempt_indexes = {
        item["name"] for item in sa.inspect(bind).get_indexes("background_job_execution_attempts")
    }
    for name, columns in (
        ("ix_background_job_execution_outbox_started", ["outbox_id", "started_at"]),
        ("ix_background_job_execution_status_started", ["status", "started_at"]),
    ):
        if name not in attempt_indexes:
            op.create_index(name, "background_job_execution_attempts", columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("background_job_execution_attempts"):
        indexes = {
            item["name"] for item in inspector.get_indexes("background_job_execution_attempts")
        }
        for name in (
            "ix_background_job_execution_status_started",
            "ix_background_job_execution_outbox_started",
        ):
            if name in indexes:
                op.drop_index(name, table_name="background_job_execution_attempts")
        op.drop_table("background_job_execution_attempts")

    if not inspector.has_table("background_job_outbox"):
        return
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("background_job_outbox")}
    for name in (
        "ix_background_job_outbox_execution_lease",
        "ix_background_job_outbox_execution_lease_token",
        "ix_background_job_outbox_last_rq_job_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="background_job_outbox")

    columns = {item["name"] for item in sa.inspect(bind).get_columns("background_job_outbox")}
    checks = {
        item.get("name") for item in sa.inspect(bind).get_check_constraints("background_job_outbox")
    }
    with op.batch_alter_table("background_job_outbox") as batch:
        if "ck_background_job_outbox_status" in checks:
            batch.drop_constraint("ck_background_job_outbox_status", type_="check")
        batch.create_check_constraint(
            "ck_background_job_outbox_status",
            "status IN ('pending','publishing','enqueued','dead')",
        )
        for name in (
            "last_execution_error_code", "completed_at", "last_started_at",
            "execution_lease_expires_at", "execution_lease_token",
            "execution_generation", "execution_attempts", "last_rq_job_id",
        ):
            if name in columns:
                batch.drop_column(name)
        for name in (
            "ck_background_job_outbox_execution_attempts",
            "ck_background_job_outbox_execution_generation",
        ):
            if name in checks:
                batch.drop_constraint(name, type_="check")
