"""outbox: status of the last refusal

Revision ID: 3b9e1c7d5a20
Revises: 74174c317dc1
Create Date: 2026-10-28 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '3b9e1c7d5a20'
down_revision: str | None = '74174c317dc1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('server_outbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_status', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('server_outbox', schema=None) as batch_op:
        batch_op.drop_column('last_status')
