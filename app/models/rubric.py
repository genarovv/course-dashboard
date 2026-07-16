import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ArtifactRole, Base, EnumColumn, RubricType


class Rubric(Base):
    __tablename__ = "rubric"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[RubricType] = mapped_column(EnumColumn(RubricType))
    artifact_role: Mapped[ArtifactRole | None] = mapped_column(EnumColumn(ArtifactRole), nullable=True)
    version: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    items: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
