"""FastAPI HTML-роуты (Jinja2). Общие шаблоны и DB-сессия — здесь."""

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.labels import (
    CONFIDENCE_LABELS,
    MR_STATE_LABELS,
    PARTIAL_LABELS,
    STATUS_LABELS,
    repo_short_name,
    role_title,
)
from app.store import SessionLocal
from app.timeutil import fmt_dt

templates = Jinja2Templates(directory=settings.template_dir)
# D8: русские подписи — общие для всех шаблонов (labels.py, один факт в одном месте);
# D19: короткое имя репо и формат времени ДД.ММ ЧЧ:ММ — тоже общие
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    PARTIAL_LABELS=PARTIAL_LABELS,
    CONFIDENCE_LABELS=CONFIDENCE_LABELS,
    MR_STATE_LABELS=MR_STATE_LABELS,
    repo_short_name=repo_short_name,
    role_title=role_title,
    fmt_dt=fmt_dt,
)


def get_session():
    """FastAPI-dependency: сессия с коммитом на успех (переопределяется в тестах)."""
    with SessionLocal() as session:
        yield session
        session.commit()
