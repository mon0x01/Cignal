"""Add durable incident operations tasks and approval history.

Revision ID: 0016_incident_operations
Revises: 0015_acq_archive_learning
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_incident_operations"
down_revision = "0015_acq_archive_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("incident_actions"):
        op.create_table(
            "incident_actions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("title", sa.String(180), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("action_type", sa.String(32), nullable=False),
            sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
            sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("approval_status", sa.String(16), nullable=False, server_default="not_required"),
            sa.Column("assigned_to", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("approval_note", sa.Text(), nullable=True),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("version >= 1", name="ck_incident_actions_version_positive"),
            sa.CheckConstraint(
                "action_type IN ('monitoring','logistics','communications','medical_support','infrastructure','public_warning','evacuation','resource_dispatch','medical_intervention','other')",
                name="ck_incident_actions_type",
            ),
            sa.CheckConstraint("priority IN ('low','normal','high','urgent')", name="ck_incident_actions_priority"),
            sa.CheckConstraint(
                "status IN ('proposed','approved','in_progress','blocked','completed','cancelled','rejected')",
                name="ck_incident_actions_status",
            ),
            sa.CheckConstraint(
                "approval_status IN ('not_required','pending','approved','rejected')",
                name="ck_incident_actions_approval_status",
            ),
            sa.CheckConstraint(
                "NOT approval_required OR status NOT IN ('approved','in_progress','blocked','completed') OR approval_status = 'approved'",
                name="ck_incident_actions_approval_gate",
            ),
        )
    inspector = sa.inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes("incident_actions")}
    for name, cols in (
        ("ix_incident_actions_incident_id", ["incident_id"]),
        ("ix_incident_actions_action_type", ["action_type"]),
        ("ix_incident_actions_priority", ["priority"]),
        ("ix_incident_actions_status", ["status"]),
        ("ix_incident_actions_assigned_to", ["assigned_to"]),
        ("ix_incident_actions_incident_status_priority", ["incident_id", "status", "priority"]),
        ("ix_incident_actions_assignee_status", ["assigned_to", "status"]),
    ):
        if name not in indexes:
            op.create_index(name, "incident_actions", cols)

    if not inspector.has_table("incident_action_events"):
        op.create_table(
            "incident_action_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("action_id", sa.String(36), sa.ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("from_status", sa.String(20), nullable=True),
            sa.Column("to_status", sa.String(20), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("event_data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("incident_action_events")}
    for name, cols in (
        ("ix_incident_action_events_action_id", ["action_id"]),
        ("ix_incident_action_events_timeline", ["action_id", "created_at", "id"]),
    ):
        if name not in indexes:
            op.create_index(name, "incident_action_events", cols)


def downgrade() -> None:
    op.drop_table("incident_action_events")
    op.drop_table("incident_actions")
