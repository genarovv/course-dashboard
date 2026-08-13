"""logging_config — единая настройка логирования приложения (issue #75).

До этого модуля root-логгер не настраивался вовсе: прикладные logger.info
не попадали в журнал (срабатывал только lastResort-хендлер WARNING+), а
warning/error печатались голой строкой без времени, уровня и источника.

Контракт (решение CEO 2026-08-13):
  * каждая запись — UTC-время, уровень, модуль-источник;
  * ошибки — с контекстом (что делали, какие данные) и трассировкой;
  * ПД/пароли/токены не пишутся (NFR-3): метки пользователей — короткий
    хеш (user_marker), а не идентификатор.
"""

import hashlib
import logging
import sys
from datetime import UTC, datetime
from typing import override

# %(name)s — модуль-источник (например app.services.sync_orchestrator)
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

#: Длина хеша-метки пользователя в гекс-символах (8 символов — достаточно,
#: чтобы отличать операторов в журнале, не раскрывая идентификатор).
_MARKER_LEN = 8

#: Логгеры, которые uvicorn настраивает своим LOGGING_CONFIG до импорта
#: приложения: uvicorn и uvicorn.access — со своими хендлерами и propagate=False,
#: uvicorn.error — пишет через родителя uvicorn.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class UtcFormatter(logging.Formatter):
    """Время записи всегда в UTC — хранение и логи живут в одном часовом поясе (#32)."""

    def __init__(self, fmt: str = _FORMAT, datefmt: str | None = _DATEFMT):
        super().__init__(fmt=fmt, datefmt=datefmt)

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        instant = datetime.fromtimestamp(record.created, tz=UTC)
        return instant.strftime(datefmt or _DATEFMT)


def user_marker(user_id: str | None) -> str | None:
    """Короткая метка оператора без ПД: первые 8 гекс-символов sha256(user_id).

    Обратная задача (user_id по метке) вычислимо не решается — в журнале нет
    данных, позволяющих сопоставить метку с человеком. None → оператор не известен.
    """
    if not user_id:
        return None
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"u_{digest[:_MARKER_LEN]}"


def configure_logging(level: int = logging.INFO) -> None:
    """Настроить root-логгер: stderr, формат «время UTC, уровень, модуль: сообщение».

    Идемпотентно: повторные вызовы (перезагрузка uvicorn, несколько импортов
    main в тестах) не плодят хендлеры. Прикладные логгеры наследуют уровень и
    хендлер от root через propagate.

    D47: логгеры uvicorn переводятся на тот же формат — их собственные хендлеры
    снимаются, включается propagate к root. Раньше их не трогали, и в journald
    жили вперемешку два вида таймстампов и записей (аудит 2026-08-14). Текст
    access-сообщения самого uvicorn не переделывается — единая только обёртка
    «время, уровень, источник».
    """
    # S76 (#76): httpx на INFO пишет строку с полным URL на каждый запрос к
    # хостингу — сотни строк на обход и адреса репозиториев с именами студентов
    # (ПД, NFR-3) в журнале. Ошибки httpx остаются видимыми (WARNING+).
    # До идемпотентного guard: уровень не хендлер, повторная установка безвредна.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # D47: uvicorn настраивает свои логгеры ДО импорта приложения, а этот код
    # выполняется при импорте app.main — момент валиден для перенастройки.
    # Снимаем их хендлеры и включаем propagate: запись поднимается к root и
    # получает UTC-время, уровень и источник от UtcFormatter. Тоже до guard:
    # операция повторно безвредна, а хендлеры uvicorn могли появиться уже после
    # первого вызова (например, переустановка конфигурации логирования).
    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    root = logging.getLogger()
    if any(getattr(h, "_cd_logging_configured", False) for h in root.handlers):
        return
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(UtcFormatter(_FORMAT, _DATEFMT))
    handler._cd_logging_configured = True  # type: ignore[attr-defined]
    root.addHandler(handler)
