import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ArtifactRole, Base, EnumColumn


class EdgeDef(Base):
    __tablename__ = "edge_def"
    __table_args__ = (UniqueConstraint("source_role", "target_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_role: Mapped[ArtifactRole] = mapped_column(EnumColumn(ArtifactRole))
    target_role: Mapped[ArtifactRole] = mapped_column(EnumColumn(ArtifactRole))
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubric.id"))
