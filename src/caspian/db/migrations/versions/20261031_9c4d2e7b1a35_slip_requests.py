"""issue slips are the goods requests: goods_requests.document_id; storekeepers see requests

Revision ID: 9c4d2e7b1a35
Revises: 5e2a8f61c0d4
Create Date: 2026-10-31 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '9c4d2e7b1a35'
down_revision: str | None = '5e2a8f61c0d4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    with op.batch_alter_table('goods_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('document_id', _ID, nullable=True))
        batch_op.create_unique_constraint(batch_op.f('uq_goods_requests_document_id'), ['document_id'])
        batch_op.create_foreign_key(batch_op.f('fk_goods_requests_document_id_documents'), 'documents',
                                    ['document_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('goods_requests', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_goods_requests_document_id_documents'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('uq_goods_requests_document_id'), type_='unique')
        batch_op.drop_column('document_id')
