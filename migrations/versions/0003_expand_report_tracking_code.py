"""Expand report tracking codes to 32 characters.

Revision ID: 0003_expand_report_tracking_code
Revises: 0002_encrypt_mfa_secrets
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_expand_report_tracking_code"
down_revision = "0002_encrypt_mfa_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.alter_column("tracking_code", existing_type=sa.String(length=24), type_=sa.String(length=32))


def downgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.alter_column("tracking_code", existing_type=sa.String(length=32), type_=sa.String(length=24))
