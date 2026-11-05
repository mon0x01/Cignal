"""Add durable per-source acquisition scheduling and retry leases.

Revision ID: 0014_acq_source_sched
Revises: 0013_source_provenance
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_acq_source_sched"
down_revision = "0013_source_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("acquisition_source_states"):
        op.create_table(
            "acquisition_source_states",
            sa.Column("source_type", sa.String(length=24), primary_key=True),
            sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="1800"),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_token", sa.String(length=36), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_id", sa.String(length=36), nullable=True),
            sa.Column("last_status", sa.String(length=20), nullable=False, server_default="not_scheduled"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error_code", sa.String(length=80), nullable=True),
            sa.Column("last_records_fetched", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_records_inserted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_records_duplicate", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_records_rejected", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_acquisition_source_states_next_run_at", "acquisition_source_states", ["next_run_at"])
        op.create_index("ix_acquisition_source_states_lease_token", "acquisition_source_states", ["lease_token"])
        op.create_index("ix_acquisition_source_states_lease_expires_at", "acquisition_source_states", ["lease_expires_at"])
        op.create_index("ix_acquisition_source_states_last_completed_at", "acquisition_source_states", ["last_completed_at"])
        op.create_index(
            "ix_acquisition_source_due", "acquisition_source_states",
            ["next_run_at", "lease_expires_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("acquisition_source_states"):
        op.drop_table("acquisition_source_states")
