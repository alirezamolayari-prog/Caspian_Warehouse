"""units.allow_decimal (#21)

Counted units (عدد، کارتن…) must hold whole quantities. The new column defaults to true
(no change for any unit) and only the units the app seeded as count units start as false;
an admin can change either in «اطلاعات پایه ← واحدها». No business data is modified.

Revision ID: c4a9e1f07b52
Revises: 8d31f0a6c2e4
Create Date: 2026-09-30 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c4a9e1f07b52'
down_revision: str | None = '8d31f0a6c2e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNT_UNITS = ("عدد", "جعبه", "کارتن", "بسته", "دست", "رول")  # as seeded (caspian.db.seed)


def upgrade() -> None:
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('allow_decimal', sa.Boolean(), server_default='1', nullable=False))
    units = sa.table('units', sa.column('name', sa.String), sa.column('allow_decimal', sa.Boolean))
    op.execute(units.update().where(units.c.name.in_(COUNT_UNITS)).values(allow_decimal=False))


def downgrade() -> None:
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.drop_column('allow_decimal')
