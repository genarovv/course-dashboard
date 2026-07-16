"""FastAPI HTML-роуты (Jinja2). Общие шаблоны и DB-сессия — здесь."""

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.store import SessionLocal

templates = Jinja2Templates(directory=settings.template_dir)


def get_session():
    """FastAPI-dependency: сессия с коммитом на успех (переопределяется в тестах)."""
    with SessionLocal() as session:
        yield session
        session.commit()
