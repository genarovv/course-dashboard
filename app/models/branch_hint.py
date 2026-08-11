import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base
from app.timeutil import utcnow


class BranchHint(Base):
    """D43 (#69): работа, найденная вне дефолтной ветки — сигнал, не вердикт.

    Append-only, как MrObservation: строка фиксирует «как было на момент обхода».
    Ветки переписывают и удаляют, выводов из их истории не делаем.

    Вердикты по этим веткам НЕ считаются: канон связности остаётся за дефолтной
    веткой. Строка существует, чтобы пустая ячейка матрицы не читалась как
    «студент ничего не сделал», когда работа лежит в соседней ветке.
    """

    __tablename__ = "branch_hint"
    # одна подсказка на ветку за обход
    __table_args__ = (UniqueConstraint("sync_run_id", "repository_id", "branch_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sync_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("sync_run.id"))
    repository_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository.id"))
    branch_name: Mapped[str] = mapped_column(String(200))
    head_sha: Mapped[str] = mapped_column(String(40))
    # сбой чтения даты не отменяет подсказку — дата свидетельство «в плюс» (как в D19)
    head_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # сколько артефактов курса нашлось в дереве этой ветки против дефолтной
    artifacts_found: Mapped[int] = mapped_column(Integer)
    artifacts_in_default: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
