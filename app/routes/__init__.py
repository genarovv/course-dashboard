"""FastAPI HTML-роуты (Jinja2). Общие шаблоны и DB-сессия — здесь."""

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.labels import CONFIDENCE_LABELS, PARTIAL_LABELS, STATUS_LABELS
from app.store import SessionLocal

templates = Jinja2Templates(directory=settings.template_dir)
# D8: русские подписи — общие для всех шаблонов (labels.py, один факт в одном месте)
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    PARTIAL_LABELS=PARTIAL_LABELS,
    CONFIDENCE_LABELS=CONFIDENCE_LABELS,
)


def get_session():
    """FastAPI-dependency: сессия с коммитом на успех (переопределяется в тестах)."""
    with SessionLocal() as session:
        yield session
        session.commit()
