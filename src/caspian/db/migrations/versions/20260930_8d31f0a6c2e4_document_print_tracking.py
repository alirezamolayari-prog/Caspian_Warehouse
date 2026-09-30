"""documents: print tracking (print_count, last_printed_at/by)

Revision ID: 8d31f0a6c2e4
Revises: 5b7e2c41d9a3
Create Date: 2026-09-30 19:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '8d31f0a6c2e4'
down_revision: str | None = '5b7e2c41d9a3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('print_count', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('last_printed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_printed_by_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'),
                                      nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_documents_last_printed_by_id_users'), 'users',
                                    ['last_printed_by_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_documents_last_printed_by_id_users'), type_='foreignkey')
        batch_op.drop_column('last_printed_by_id')
        batch_op.drop_column('last_printed_at')
        batch_op.drop_column('print_count')
