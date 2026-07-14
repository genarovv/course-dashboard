import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, ConfidenceLevel, DeferredReason, EnumColumn, VerdictValue


class CoherenceVerdict(Base):
    __tablename__ = "coherence_verdict"
    __table_args__ = (
        Index(
            "idx_quad",
            "source_content_hash",
            "target_content_hash",
            "rubric_id",
            "llm_model",
            unique=True,
            sqlite_where="verdict != 'deferred'",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    edge_def_id: Mapped[str] = mapped_column(String(36), ForeignKey("edge_def.id"))
    source_snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact_snapshot.id"))
    target_snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact_snapshot.id"))
    source_content_hash: Mapped[str] = mapped_column(String(64))
    target_content_hash: Mapped[str] = mapped_column(String(64))
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubric.id"))
    llm_model: Mapped[str] = mapped_column(String(100))
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    verdict: Mapped[VerdictValue] = mapped_column(EnumColumn(VerdictValue))
    deferred_reason: Mapped[DeferredReason | None] = mapped_column(EnumColumn(DeferredReason), nullable=True)
    confidence: Mapped[ConfidenceLevel] = mapped_column(EnumColumn(ConfidenceLevel))
    entities_checked: Mapped[int] = mapped_column(default=0)
    entities_found: Mapped[int] = mapped_column(default=0)
    entities_excluded: Mapped[int] = mapped_column(default=0)
    entities_lost: Mapped[int] = mapped_column(default=0)
    points: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
