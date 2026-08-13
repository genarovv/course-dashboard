"""D47: логи uvicorn идут в едином формате #75 — UTC-время, уровень, источник.

До этого тикета configure_logging логгеры uvicorn не трогал: их собственные
хендлеры писали «INFO:     127.0.0.1:40594 - "POST /sync HTTP/1.1" 200 OK»
без времени, и в journald жили вперемешку два вида таймстампов и записей
(аудит боевого журнала 2026-08-14).

Контракт: configure_logging снимает собственные хендлеры логгеров uvicorn
(uvicorn, uvicorn.error, uvicorn.access) и включает propagate — каждая запись
проходит через root-хендлер с UtcFormatter. Текст access-сообщения самого
uvicorn не переделывается: единая только обёртка «время, уровень, источник».

Логгеры uvicorn здесь настраиваются вручную — так же, как это делает сам
uvicorn до импорта приложения; реальный сервер тесты не поднимают.
"""

import logging
import re

import pytest

from app.logging_config import UtcFormatter, configure_logging

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
ACCESS_LINE = '127.0.0.1:40594 - "POST /sync HTTP/1.1" 200 OK'


class _ListHandler(logging.Handler):
    """Собирает записи в список — root мог быть переопределён fileConfig алембика
    (см. tests/test_logging.py), поэтому проверяем через собственный хендлер."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _simulate_uvicorn_setup() -> None:
    """Воспроизводит дефолтный LOGGING_CONFIG uvicorn: у uvicorn и uvicorn.access —
    свои хендлеры и propagate=False, уровни INFO. Именно это состояние застаёт
    configure_logging при импорте app.main под uvicorn."""
    for name in ("uvicorn", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [logging.NullHandler()]
        lg.propagate = False
        lg.setLevel(logging.INFO)
    # uvicorn.error у uvicorn без своих хендлеров — пишет через родителя uvicorn
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)


@pytest.fixture(autouse=True)
def _restore_uvicorn_loggers():
    """Тесты правят глобальные логгеры uvicorn — вернуть исходное состояние,
    чтобы не фонить на остальную сьюту."""
    yield
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(logging.NOTSET)


def test_uvicorn_handlers_removed_and_propagate_enabled():
    _simulate_uvicorn_setup()
    configure_logging()
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        assert lg.handlers == [], f"{name}: собственные хендлеры должны быть сняты"
        assert lg.propagate, f"{name}: записи должны подниматься к root"


def test_access_record_reaches_root_in_unified_format():
    """Access-запись доходит до root и форматируется как прикладная: UTC-время,
    уровень, источник uvicorn.access — а сам текст access-строки не переделан."""
    _simulate_uvicorn_setup()
    configure_logging()
    capture = _ListHandler()
    capture.setFormatter(UtcFormatter())
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        logging.getLogger("uvicorn.access").info(ACCESS_LINE)
    finally:
        root.removeHandler(capture)

    assert len(capture.records) == 1
    line = capture.format(capture.records[0])
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+0000 INFO uvicorn\.access: ", line)
    assert line.endswith(ACCESS_LINE)


def test_repeated_call_strips_handlers_readded_after_first_call():
    """Идемпотентность: guard root-хендлера не должен мешать повторному вызову
    снова снять хендлеры uvicorn (например, если конфигурация логгеров была
    переустановлена позже первого configure_logging)."""
    configure_logging()
    _simulate_uvicorn_setup()  # uvicorn «вернул» свои хендлеры после первого вызова
    configure_logging()

    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        assert lg.handlers == [], f"{name}: повторный вызов должен снять хендлеры"
        assert lg.propagate, name
    configured = [
        h for h in logging.getLogger().handlers if getattr(h, "_cd_logging_configured", False)
    ]
    assert len(configured) == 1  # root-хендлер при этом не задублирован
