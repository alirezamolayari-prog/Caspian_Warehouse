"""personal Caspian Server accounts: users linked to the server, authors of queued rows

Revision ID: 5e2a8f61c0d4
Revises: 3b9e1c7d5a20
Create Date: 2026-10-29 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5e2a8f61c0d4'
down_revision: str | None = '3b9e1c7d5a20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('server_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('mobile', sa.String(length=11), nullable=True))
        batch_op.add_column(sa.Column('server_full_name', sa.String(length=150), server_default='',
                                      nullable=False))
        batch_op.create_unique_constraint(batch_op.f('uq_users_server_user_id'), ['server_user_id'])
        batch_op.create_unique_constraint(batch_op.f('uq_users_mobile'), ['mobile'])
    with op.batch_alter_table('goods_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by_server_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('created_by_name', sa.String(length=150), server_default='',
                                      nullable=False))
    with op.batch_alter_table('goods_request_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', _ID, nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_goods_request_messages_user_id_users'), 'users',
                                    ['user_id'], ['id'])
    with op.batch_alter_table('server_outbox', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', _ID, nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_server_outbox_user_id_users'), 'users',
                                    ['user_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('server_outbox', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_server_outbox_user_id_users'), type_='foreignkey')
        batch_op.drop_column('user_id')
    with op.batch_alter_table('goods_request_messages', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_goods_request_messages_user_id_users'), type_='foreignkey')
        batch_op.drop_column('user_id')
    with op.batch_alter_table('goods_requests', schema=None) as batch_op:
        batch_op.drop_column('created_by_name')
        batch_op.drop_column('created_by_server_id')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('uq_users_mobile'), type_='unique')
        batch_op.drop_constraint(batch_op.f('uq_users_server_user_id'), type_='unique')
        batch_op.drop_column('server_full_name')
        batch_op.drop_column('mobile')
        batch_op.drop_column('server_user_id')
