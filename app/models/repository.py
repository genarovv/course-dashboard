import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, EnumColumn, GitHost


class Repository(Base):
    __tablename__ = "repository"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_url: Mapped[str] = mapped_column(String(500))
    normalized_repo_url: Mapped[str] = mapped_column(String(500), unique=True)
    git_host: Mapped[GitHost] = mapped_column(EnumColumn(GitHost))
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    added_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
