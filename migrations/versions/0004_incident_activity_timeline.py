"""Add append-only incident activity timeline.

Revision ID: 0004_incident_activity_timeline
Revises: 0003_expand_report_tracking_code
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_incident_activity_timeline"
down_revision = "0003_expand_report_tracking_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001_initial_schema calls current Base.metadata.create_all(). On a brand-new
    # database that may already create this table, so guard for both upgrade paths.
    bind = op.get_bind()
    if sa.inspect(bind).has_table("incident_activities"):
        return

    op.create_table(
        "incident_activities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_display_name", sa.String(length=120), nullable=False),
        sa.Column("actor_role", sa.String(length=24), nullable=False),
        sa.Column("activity_type", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("location_text", sa.String(length=220), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incident_activities_incident_id", "incident_activities", ["incident_id"])
    op.create_index("ix_incident_activities_actor_user_id", "incident_activities", ["actor_user_id"])
    op.create_index("ix_incident_activities_activity_type", "incident_activities", ["activity_type"])
    op.create_index("ix_incident_activities_created_at", "incident_activities", ["created_at"])
    op.create_index(
        "ix_incident_activity_timeline", "incident_activities",
        ["incident_id", "created_at", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("incident_activities"):
        op.drop_table("incident_activities")
