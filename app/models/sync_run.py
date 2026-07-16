import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, SyncStatus, SyncTrigger


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    triggered_by: Mapped[SyncTrigger] = mapped_column(EnumColumn(SyncTrigger))
    status: Mapped[SyncStatus] = mapped_column(EnumColumn(SyncStatus), default=SyncStatus.in_progress)
