"""seed floors and consumption locations from data/seed/caspian_locations.csv (spec Part 2)

Data migration. Inserts only missing codes, so running it again never duplicates anything and
never overwrites what an admin changed later. Downgrade leaves the rows: the previous
revision's downgrade drops the tables themselves.

Revision ID: 7f3c2a9d41e6
Revises: 225596383a11
Create Date: 2026-10-11 18:00:00
"""
from collections.abc import Sequence

from alembic import op

from caspian.db import location_seed

revision: str = '7f3c2a9d41e6'
down_revision: str | None = '225596383a11'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    location_seed.seed(op.get_bind(), location_seed.read_seed())


def downgrade() -> None:
    pass
