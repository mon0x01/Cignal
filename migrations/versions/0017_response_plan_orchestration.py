"""Add versioned incident response plans and task dependencies.

Revision ID: 0017_response_plan_orchestration
Revises: 0016_incident_operations
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_response_plan_orchestration"
down_revision = "0016_incident_operations"
branch_labels = None
depends_on = None


def _create_index_if_missing(name: str, table: str, columns: list[str], **kwargs) -> None:
    inspector = sa.inspect(op.get_bind())
    if name not in {item["name"] for item in inspector.get_indexes(table)}:
        op.create_index(name, table, columns, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("incident_response_plans"):
        op.create_table(
            "incident_response_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("template_key", sa.String(64), nullable=False),
            sa.Column("template_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("title", sa.String(180), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("template_snapshot", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "incident_id", "idempotency_key", name="uq_incident_response_plans_idempotency"
            ),
            sa.CheckConstraint("template_version >= 1", name="ck_response_plan_template_version"),
            sa.CheckConstraint(
                "status IN ('active','completed','cancelled','needs_review')",
                name="ck_response_plan_status",
            ),
        )
    _create_index_if_missing(
        "ix_incident_response_plans_incident_id", "incident_response_plans", ["incident_id"]
    )
    _create_index_if_missing(
        "ix_incident_response_plans_created_by", "incident_response_plans", ["created_by"]
    )
    _create_index_if_missing(
        "ix_incident_response_plans_status", "incident_response_plans", ["status"]
    )
    _create_index_if_missing(
        "ix_incident_response_plans_incident_created", "incident_response_plans",
        ["incident_id", "created_at"],
    )
    _create_index_if_missing(
        "uq_incident_response_plans_active_incident", "incident_response_plans", ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    action_columns = {item["name"] for item in sa.inspect(bind).get_columns("incident_actions")}
    missing_action_columns = {"response_plan_id", "sequence_order"} - action_columns
    if bind.dialect.name == "sqlite":
        # SQLite permits ADD COLUMN with a nullable REFERENCES column, but
        # Alembic's generic add_column path attempts a separate ALTER CONSTRAINT.
        # Use native ALTER TABLE so existing action-event foreign keys and rows
        # are never disrupted by a batch table rebuild.
        if "response_plan_id" in missing_action_columns:
            bind.exec_driver_sql(
                "ALTER TABLE incident_actions ADD COLUMN response_plan_id VARCHAR(36) "
                "REFERENCES incident_response_plans(id) ON DELETE SET NULL"
            )
        if "sequence_order" in missing_action_columns:
            bind.exec_driver_sql(
                "ALTER TABLE incident_actions ADD COLUMN sequence_order INTEGER NOT NULL DEFAULT 0"
            )
    else:
        if "response_plan_id" in missing_action_columns:
            op.add_column(
                "incident_actions",
                sa.Column(
                    "response_plan_id", sa.String(36),
                    sa.ForeignKey("incident_response_plans.id", name="fk_incident_actions_response_plan_id", ondelete="SET NULL"), nullable=True,
                ),
            )
        if "sequence_order" in missing_action_columns:
            op.add_column(
                "incident_actions",
                sa.Column("sequence_order", sa.Integer(), nullable=False, server_default="0"),
            )
    _create_index_if_missing(
        "ix_incident_actions_response_plan_id", "incident_actions", ["response_plan_id"]
    )
    _create_index_if_missing(
        "uq_incident_actions_plan_sequence", "incident_actions",
        ["response_plan_id", "sequence_order"], unique=True,
    )

    if not sa.inspect(bind).has_table("incident_action_dependencies"):
        op.create_table(
            "incident_action_dependencies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("plan_id", sa.String(36), sa.ForeignKey("incident_response_plans.id", ondelete="CASCADE"), nullable=False),
            sa.Column("action_id", sa.String(36), sa.ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("depends_on_action_id", sa.String(36), sa.ForeignKey("incident_actions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "plan_id", "action_id", "depends_on_action_id",
                name="uq_incident_action_dependencies_unique",
            ),
            sa.CheckConstraint("action_id <> depends_on_action_id", name="ck_action_dependency_not_self"),
        )
    _create_index_if_missing(
        "ix_incident_action_dependencies_plan_id", "incident_action_dependencies", ["plan_id"]
    )
    _create_index_if_missing(
        "ix_incident_action_dependencies_action_id", "incident_action_dependencies", ["action_id"]
    )
    _create_index_if_missing(
        "ix_incident_action_dependencies_depends_on_action_id",
        "incident_action_dependencies", ["depends_on_action_id"],
    )
    _create_index_if_missing(
        "ix_incident_action_dependencies_plan_action", "incident_action_dependencies",
        ["plan_id", "action_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("incident_action_dependencies"):
        op.drop_table("incident_action_dependencies")
    if inspector.has_table("incident_actions"):
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("incident_actions")}
        for name in ("uq_incident_actions_plan_sequence", "ix_incident_actions_response_plan_id"):
            if name in indexes:
                op.drop_index(name, table_name="incident_actions")
        columns = {item["name"] for item in sa.inspect(bind).get_columns("incident_actions")}
        for name in ("sequence_order", "response_plan_id"):
            if name in columns:
                op.drop_column("incident_actions", name)
    if sa.inspect(bind).has_table("incident_response_plans"):
        op.drop_table("incident_response_plans")
