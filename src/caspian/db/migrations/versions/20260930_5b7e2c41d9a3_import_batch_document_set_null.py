"""import_batches.result_document_id: ON DELETE SET NULL

Deleting a draft that an import created failed with MariaDB error 1451 (#3).

Revision ID: 5b7e2c41d9a3
Revises: 986638137277
Create Date: 2026-09-30 18:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '5b7e2c41d9a3'
down_revision: str | None = '986638137277'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = 'fk_import_batches_result_document_id_documents'


def _recreate(ondelete: str | None) -> None:
    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.drop_constraint(FK, type_='foreignkey')
        batch_op.create_foreign_key(FK, 'documents', ['result_document_id'], ['id'], ondelete=ondelete)


def upgrade() -> None:
    _recreate('SET NULL')


def downgrade() -> None:
    _recreate(None)
