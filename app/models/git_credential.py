import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, GitHost


class GitCredential(Base):
    __tablename__ = "git_credential"
    __table_args__ = (UniqueConstraint("git_host"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    git_host: Mapped[GitHost] = mapped_column(EnumColumn(GitHost))
    is_valid: Mapped[bool] = mapped_column(default=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
