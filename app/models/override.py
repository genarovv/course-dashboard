import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Override(Base):
    __tablename__ = "override"
    __table_args__ = (
        CheckConstraint(
            "(coherence_verdict_id IS NOT NULL AND step_quality_card_id IS NULL) OR "
            "(coherence_verdict_id IS NULL AND step_quality_card_id IS NOT NULL)",
            name="ck_override_xor",
        ),
        # И4 — одна активная отметка на находку (те же индексы, что в миграции d408a1d4f3e7)
        Index(
            "idx_active_ovr_coherence",
            "coherence_verdict_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND coherence_verdict_id IS NOT NULL"),
        ),
        Index(
            "idx_active_ovr_step",
            "step_quality_card_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND step_quality_card_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    coherence_verdict_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("coherence_verdict.id"), nullable=True
    )
    step_quality_card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
