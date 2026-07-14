import uuid
from datetime import date

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Lesson(Base):
    __tablename__ = "lesson"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    number: Mapped[int] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(String(200))
    date: Mapped[date] = mapped_column(Date)
