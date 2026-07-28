import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.timeutil import utcnow


class MrObservation(Base):
    """Наблюдение MR/PR за обход — журнал канала сдачи (FR-12, ADR-007; DM §1.14).

    Append-only: ветки MR переписываемы и удаляемы — строка фиксирует
    «как было на момент обхода». Выводы из порядка коммитов не делаются.
    """

    __tablename__ = "mr_observation"
    # И12 — одно наблюдение MR за обход
    __table_args__ = (UniqueConstraint("sync_run_id", "repository_id", "mr_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sync_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("sync_run.id"))
    repository_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository.id"))
    mr_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    source_branch: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(10))  # opened | merged | closed
    reviewer_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    markers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
