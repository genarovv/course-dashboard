"""content_probes: конфиг проб у ArtifactDef + probe_findings у ArtifactSnapshot (T2, #44)

Узкая редакция (решение CEO 2026-07-28, развилка 4): проба — regex к содержимому
артефакта, только к объявленным требованиям; результат — признак карточки,
статус ячейки не меняется (BR-3 нетронут).

Revision ID: b3f1a92c7d10
Revises: 9e51c07d2af4
Create Date: 2026-07-30
"""

import sqlalchemy as sa

from alembic import op

revision = "b3f1a92c7d10"
down_revision = "9e51c07d2af4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_def") as batch:
        batch.add_column(sa.Column("content_probes", sa.JSON(), nullable=True))
    with op.batch_alter_table("artifact_snapshot") as batch:
        batch.add_column(sa.Column("probe_findings", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact_snapshot") as batch:
        batch.drop_column("probe_findings")
    with op.batch_alter_table("artifact_def") as batch:
        batch.drop_column("content_probes")
