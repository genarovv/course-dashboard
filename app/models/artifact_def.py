import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ArtifactRole, Base, EnumColumn


class ArtifactDef(Base):
    __tablename__ = "artifact_def"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    lesson_id: Mapped[str] = mapped_column(String(36), ForeignKey("lesson.id"))
    role: Mapped[ArtifactRole] = mapped_column(EnumColumn(ArtifactRole))
    expected_pattern: Mapped[str] = mapped_column(String(200))
    template_relative_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
