import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, SnapshotStatus


class ArtifactSnapshot(Base):
    __tablename__ = "artifact_snapshot"
    __table_args__ = (UniqueConstraint("sync_run_id", "repository_id", "artifact_def_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sync_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("sync_run.id"))
    repository_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository.id"))
    artifact_def_id: Mapped[str] = mapped_column(String(36), ForeignKey("artifact_def.id"))
    status: Mapped[SnapshotStatus] = mapped_column(EnumColumn(SnapshotStatus))
    partial_reason: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
