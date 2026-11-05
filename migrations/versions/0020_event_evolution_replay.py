"""Add immutable-input event evolution replay runs and results.

Revision ID: 0020_event_evolution_replay
Revises: 0019_continuous_event_discovery
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_event_evolution_replay"
down_revision = "0019_continuous_event_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("event_replay_runs"):
        op.create_table(
            "event_replay_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("engine_version", sa.String(40), nullable=False),
            sa.Column("scoring_version", sa.String(40), nullable=False),
            sa.Column("input_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("input_manifest_sha256", sa.String(64), nullable=False),
            sa.Column("baseline_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("baseline_manifest_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summary", sa.JSON(), nullable=False),
            sa.Column("failure_code", sa.String(80), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_event_replay_runs_status"),
            sa.CheckConstraint("input_count >= 0 AND baseline_count >= 0 AND result_count >= 0", name="ck_event_replay_runs_counts"),
            sa.CheckConstraint("start_at < end_at", name="ck_event_replay_runs_window"),
        )
    inspector = sa.inspect(bind)
    if "ix_event_replay_runs_status_created" not in {i["name"] for i in inspector.get_indexes("event_replay_runs")}:
        op.create_index("ix_event_replay_runs_status_created", "event_replay_runs", ["status", "created_at"])

    if not sa.inspect(bind).has_table("event_replay_inputs"):
        op.create_table(
            "event_replay_inputs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("report_id", sa.String(36), nullable=False),
            sa.Column("input_sha256", sa.String(64), nullable=False),
            sa.Column("input_data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "report_id", name="uq_event_replay_input_report"),
        )
    if "ix_event_replay_inputs_run_id" not in {i["name"] for i in sa.inspect(bind).get_indexes("event_replay_inputs")}:
        op.create_index("ix_event_replay_inputs_run_id", "event_replay_inputs", ["run_id"])

    if not sa.inspect(bind).has_table("event_replay_baselines"):
        op.create_table(
            "event_replay_baselines",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("hypothesis_id", sa.String(36), nullable=False),
            sa.Column("hypothesis_status", sa.String(20), nullable=False),
            sa.Column("scoring_version", sa.String(40), nullable=False),
            sa.Column("report_ids", sa.JSON(), nullable=False),
            sa.Column("baseline_sha256", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "hypothesis_id", name="uq_event_replay_baseline_hypothesis"),
        )
    if "ix_event_replay_baselines_run_id" not in {i["name"] for i in sa.inspect(bind).get_indexes("event_replay_baselines")}:
        op.create_index("ix_event_replay_baselines_run_id", "event_replay_baselines", ["run_id"])

    if not sa.inspect(bind).has_table("event_replay_results"):
        op.create_table(
            "event_replay_results",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("event_replay_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("candidate_key", sa.String(64), nullable=False),
            sa.Column("report_ids", sa.JSON(), nullable=False),
            sa.Column("disaster_type", sa.String(32), nullable=False),
            sa.Column("centroid_latitude", sa.Float(), nullable=False),
            sa.Column("centroid_longitude", sa.Float(), nullable=False),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("report_count", sa.Integer(), nullable=False),
            sa.Column("distinct_source_count", sa.Integer(), nullable=False),
            sa.Column("match_score", sa.Float(), nullable=False),
            sa.Column("minimum_pair_score", sa.Float(), nullable=False),
            sa.Column("comparison_kind", sa.String(24), nullable=False),
            sa.Column("matched_hypothesis_id", sa.String(36), nullable=True),
            sa.Column("overlap_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("explanation", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "candidate_key", name="uq_event_replay_result_candidate"),
            sa.UniqueConstraint("run_id", "ordinal", name="uq_event_replay_result_ordinal"),
            sa.CheckConstraint("report_count >= 2 AND distinct_source_count >= 2", name="ck_event_replay_result_counts"),
            sa.CheckConstraint("match_score >= 0 AND match_score <= 1", name="ck_event_replay_result_score"),
            sa.CheckConstraint("overlap_score >= 0 AND overlap_score <= 1", name="ck_event_replay_result_overlap"),
            sa.CheckConstraint("comparison_kind IN ('new_candidate','unchanged','expanded','contracted','diverged')", name="ck_event_replay_result_comparison"),
        )
    if "ix_event_replay_results_run_ordinal" not in {i["name"] for i in sa.inspect(bind).get_indexes("event_replay_results")}:
        op.create_index("ix_event_replay_results_run_ordinal", "event_replay_results", ["run_id", "ordinal"])


def downgrade() -> None:
    op.drop_table("event_replay_results")
    op.drop_table("event_replay_baselines")
    op.drop_table("event_replay_inputs")
    op.drop_table("event_replay_runs")
