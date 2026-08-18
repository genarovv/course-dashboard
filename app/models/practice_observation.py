import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, PracticeStatus
from app.timeutil import utcnow


class PracticeObservation(Base):
    """Проверка применения приёма курса за обход — журнал (FR-14 этап 1, #80).

    Append-only, как MrObservation: история коммитов и MR переписываемы, строка
    фиксирует «как было на момент обхода». Текущее состояние = последняя строка
    по (repository, check_key). Сами проверки — не сущности БД: они читаются из
    config.yaml на лету (как process_markers), поэтому check_key — строка, не FK.

    Это наблюдение с доказательствами, не вердикт и не оценка (BR-2): выводы
    делает преподаватель.
    """

    __tablename__ = "practice_observation"
    # одна проверка одного приёма за обход (аналог И12)
    __table_args__ = (UniqueConstraint("sync_run_id", "repository_id", "check_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sync_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("sync_run.id"))
    repository_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository.id"))
    check_key: Mapped[str] = mapped_column(String(50))
    status: Mapped[PracticeStatus] = mapped_column(EnumColumn(PracticeStatus))
    # список {kind, mr_number?, sha?, quote?, path?} — цитаты и адреса доказательств
    evidence: Mapped[list | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
