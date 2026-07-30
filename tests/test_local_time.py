"""#32: «Актуально на» — в местном времени, хранение в UTC.

Симптом: SyncRun.started_at пишется в UTC, матрица показывала UTC без метки —
преподаватель в UTC+4 видел время «4 часа назад» (ломает FR-8 «устаревание видимо»).
Решение: хранение как было (наивный UTC), рендер со смещением CD_TZ_OFFSET_MINUTES
(по умолчанию — смещение сервера) и явной меткой зоны.
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.config import settings
from app.models import SyncTrigger
from app.services.matrix_builder import build_matrix
from app.timeutil import offset_label, to_display, utcnow


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_utcnow_is_naive_utc_without_deprecation(recwarn):
    now = utcnow()
    assert now.tzinfo is None  # хранение остаётся наивным UTC (совместимо с БД)
    assert not [w for w in recwarn if "utcnow" in str(w.message)]


def test_to_display_applies_offset(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_minutes", 240)  # UTC+4, Тбилиси
    utc = datetime(2026, 7, 28, 16, 7, 0)
    assert to_display(utc).strftime("%H:%M") == "20:07"


def test_offset_label_formats(monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_minutes", 240)
    assert offset_label() == "UTC+4"
    monkeypatch.setattr(settings, "tz_offset_minutes", 330)  # Гоа
    assert offset_label() == "UTC+5:30"
    monkeypatch.setattr(settings, "tz_offset_minutes", 0)
    assert offset_label() == "UTC"


def test_matrix_as_of_in_local_time_with_label(session, monkeypatch):
    monkeypatch.setattr(settings, "tz_offset_minutes", 240)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    run.started_at = datetime(2026, 7, 28, 16, 7, 0)  # наивный UTC в БД
    session.flush()

    matrix = build_matrix(session)

    # местное время + явная метка зоны; дата добавлена решением CEO 2026-07-30 (D8)
    assert matrix["as_of"] == "28.07 20:07 (UTC+4)"
