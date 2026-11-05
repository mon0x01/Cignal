"""Persist reviewable, cross-source event hypotheses.

Revision ID: 0010_event_hypotheses
Revises: 0009_analysis_retry_v1
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_event_hypotheses"
down_revision = "0009_analysis_retry_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("event_hypotheses"):
        op.create_table(
            "event_hypotheses",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("event_key", sa.String(length=64), nullable=False, unique=True),
            sa.Column("anchor_report_id", sa.String(length=36), sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True, unique=True),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("disaster_type", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="candidate"),
            sa.Column("centroid_latitude", sa.Float(), nullable=False),
            sa.Column("centroid_longitude", sa.Float(), nullable=False),
            sa.Column("location_text", sa.Text(), nullable=True),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("distinct_source_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("match_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("scoring_version", sa.String(length=32), nullable=False),
            sa.Column("explanation", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    inspector = sa.inspect(bind)
    if not inspector.has_table("event_hypothesis_reports"):
        op.create_table(
            "event_hypothesis_reports",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("hypothesis_id", sa.String(length=36), sa.ForeignKey("event_hypotheses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("report_id", sa.String(length=36), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
            sa.Column("association_status", sa.String(length=16), nullable=False, server_default="proposed"),
            sa.Column("relationship_type", sa.String(length=16), nullable=False, server_default="supports"),
            sa.Column("pair_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("component_scores", sa.JSON(), nullable=False),
            sa.Column("reasons", sa.JSON(), nullable=False),
            sa.Column("reviewed_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("hypothesis_id", "report_id", name="uq_event_hypothesis_report"),
        )
    inspector = sa.inspect(bind)
    if not inspector.has_table("event_discovery_runs"):
        op.create_table(
            "event_discovery_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("requested_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("reports_scanned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("clusters_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("hypotheses_created", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("hypotheses_updated", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("clusters_skipped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_code", sa.String(length=80), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_hypotheses")}
    if "ix_event_hypotheses_disaster_type" not in indexes:
        op.create_index("ix_event_hypotheses_disaster_type", "event_hypotheses", ["disaster_type"])
    if "ix_event_hypotheses_status" not in indexes:
        op.create_index("ix_event_hypotheses_status", "event_hypotheses", ["status"])
    if "ix_event_hypotheses_first_observed_at" not in indexes:
        op.create_index("ix_event_hypotheses_first_observed_at", "event_hypotheses", ["first_observed_at"])
    if "ix_event_hypotheses_last_observed_at" not in indexes:
        op.create_index("ix_event_hypotheses_last_observed_at", "event_hypotheses", ["last_observed_at"])
    if "ix_event_hypotheses_status_last_observed" not in indexes:
        op.create_index("ix_event_hypotheses_status_last_observed", "event_hypotheses", ["status", "last_observed_at"])
    if "ix_event_hypotheses_created_at" not in indexes:
        op.create_index("ix_event_hypotheses_created_at", "event_hypotheses", ["created_at"])

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_hypothesis_reports")}
    if "ix_event_hypothesis_reports_hypothesis_id" not in indexes:
        op.create_index("ix_event_hypothesis_reports_hypothesis_id", "event_hypothesis_reports", ["hypothesis_id"])
    if "ix_event_hypothesis_reports_report_id" not in indexes:
        op.create_index("ix_event_hypothesis_reports_report_id", "event_hypothesis_reports", ["report_id"])
    if "ix_event_hypothesis_reports_association_status" not in indexes:
        op.create_index("ix_event_hypothesis_reports_association_status", "event_hypothesis_reports", ["association_status"])
    if "ix_event_hypothesis_reports_review" not in indexes:
        op.create_index("ix_event_hypothesis_reports_review", "event_hypothesis_reports", ["hypothesis_id", "association_status"])

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("event_discovery_runs")}
    if "ix_event_discovery_runs_status" not in indexes:
        op.create_index("ix_event_discovery_runs_status", "event_discovery_runs", ["status"])
    if "ix_event_discovery_runs_status_created" not in indexes:
        op.create_index("ix_event_discovery_runs_status_created", "event_discovery_runs", ["status", "created_at"])
    if "ix_event_discovery_runs_created_at" not in indexes:
        op.create_index("ix_event_discovery_runs_created_at", "event_discovery_runs", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("event_discovery_runs"):
        op.drop_table("event_discovery_runs")
    if inspector.has_table("event_hypothesis_reports"):
        op.drop_table("event_hypothesis_reports")
    if inspector.has_table("event_hypotheses"):
        op.drop_table("event_hypotheses")
