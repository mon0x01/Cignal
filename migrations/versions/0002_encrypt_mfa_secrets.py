"""Allow encrypted TOTP secret values.

Revision ID: 0002_encrypt_mfa_secrets
Revises: 0001_initial_schema
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_encrypt_mfa_secrets"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("mfa_secret", existing_type=sa.String(length=64), type_=sa.String(length=255))
        batch.alter_column("pending_mfa_secret", existing_type=sa.String(length=64), type_=sa.String(length=255))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("mfa_secret", existing_type=sa.String(length=255), type_=sa.String(length=64))
        batch.alter_column("pending_mfa_secret", existing_type=sa.String(length=255), type_=sa.String(length=64))
