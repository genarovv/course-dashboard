from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, SyncOutcome


class SyncRunRepository(Base):
    __tablename__ = "sync_run_repository"
    __table_args__ = (UniqueConstraint("sync_run_id", "repository_id"),)

    sync_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("sync_run.id"), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository.id"), primary_key=True)
    outcome: Mapped[SyncOutcome] = mapped_column(EnumColumn(SyncOutcome))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
