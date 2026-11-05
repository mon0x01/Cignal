"""Add governed event-pair labels and immutable research dataset versions.

Revision ID: 0012_research_data_v1
Revises: 0011_event_intel_v2
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_research_data_v1"
down_revision = "0011_event_intel_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("research_datasets"):
        op.create_table(
            "research_datasets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("slug", sa.String(80), nullable=False, unique=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("task_type", sa.String(48), nullable=False),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("license_identifier", sa.String(120), nullable=False),
            sa.Column("license_url", sa.String(700), nullable=True),
            sa.Column("legal_basis", sa.String(40), nullable=False),
            sa.Column("consent_scope", sa.String(40), nullable=False),
            sa.Column("source_types", sa.JSON(), nullable=False),
            sa.Column("coordinate_precision", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("privacy_policy", sa.String(48), nullable=False, server_default="exclude_direct_identifiers"),
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("event_pair_labels"):
        op.create_table(
            "event_pair_labels",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("report_a_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
            sa.Column("report_b_id", sa.String(36), sa.ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
            sa.Column("pair_fingerprint", sa.String(64), nullable=False),
            sa.Column("label", sa.String(20), nullable=False),
            sa.Column("split_group", sa.String(120), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("label_method", sa.String(32), nullable=False, server_default="analyst_review"),
            sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("privacy_review_status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("provenance_snapshot", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("dataset_id", "pair_fingerprint", name="uq_event_pair_label_fingerprint"),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("research_dataset_versions"):
        op.create_table(
            "research_dataset_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("dataset_id", sa.String(36), sa.ForeignKey("research_datasets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="sealed"),
            sa.Column("schema_version", sa.String(40), nullable=False),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("same_event_count", sa.Integer(), nullable=False),
            sa.Column("different_event_count", sa.Integer(), nullable=False),
            sa.Column("split_group_count", sa.Integer(), nullable=False),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("dataset_id", "version_number", name="uq_research_dataset_version"),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("research_dataset_version_items"):
        op.create_table(
            "research_dataset_version_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("version_id", sa.String(36), sa.ForeignKey("research_dataset_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("pair_fingerprint", sa.String(64), nullable=False),
            sa.Column("split_group", sa.String(120), nullable=False),
            sa.Column("label", sa.String(20), nullable=False),
            sa.Column("record_json", sa.JSON(), nullable=False),
            sa.Column("provenance_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("version_id", "ordinal", name="uq_research_dataset_version_ordinal"),
            sa.UniqueConstraint("version_id", "pair_fingerprint", name="uq_research_dataset_version_pair"),
        )

    for table, index_name, columns in (
        ("research_datasets", "ix_research_datasets_slug", ["slug"]),
        ("research_datasets", "ix_research_datasets_status", ["status"]),
        ("research_datasets", "ix_research_datasets_status_updated", ["status", "updated_at"]),
        ("event_pair_labels", "ix_event_pair_labels_review_status", ["review_status"]),
        ("event_pair_labels", "ix_event_pair_labels_privacy_review_status", ["privacy_review_status"]),
        ("event_pair_labels", "ix_event_pair_labels_dataset_review", ["dataset_id", "review_status", "privacy_review_status"]),
        ("event_pair_labels", "ix_event_pair_labels_dataset_group", ["dataset_id", "split_group"]),
        ("research_dataset_versions", "ix_research_dataset_versions_dataset_id", ["dataset_id"]),
        ("research_dataset_versions", "ix_research_dataset_versions_dataset_created", ["dataset_id", "created_at"]),
        ("research_dataset_version_items", "ix_research_dataset_version_items_version_id", ["version_id"]),
        ("research_dataset_version_items", "ix_research_dataset_version_items_group", ["version_id", "split_group"]),
    ):
        existing = {item["name"] for item in sa.inspect(bind).get_indexes(table)}
        if index_name not in existing:
            op.create_index(index_name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "research_dataset_version_items",
        "research_dataset_versions",
        "event_pair_labels",
        "research_datasets",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
