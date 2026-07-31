"""timeutil — время проекта (#32).

Контракт: в БД всегда наивный UTC (совместимо с существующими данными и
сравнениями), наружу — местное время с явной меткой зоны. Смещение задаёт
CD_TZ_OFFSET_MINUTES; по умолчанию берётся смещение сервера.
"""

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Наивный UTC «сейчас» — замена устаревшего datetime.utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)


def _offset_minutes() -> int:
    from app.config import settings

    if settings.tz_offset_minutes is not None:
        return settings.tz_offset_minutes
    local_offset = datetime.now().astimezone().utcoffset() or timedelta()
    return int(local_offset.total_seconds() // 60)


def to_display(utc_naive: datetime) -> datetime:
    """Наивный UTC из БД → местное время для отображения."""
    return utc_naive + timedelta(minutes=_offset_minutes())


def fmt_dt(value: datetime | str | None) -> str:
    """D19 (#65): таймстемп для интерфейса — ДД.ММ ЧЧ:ММ, без микросекунд.

    Принимает datetime или ISO-строку (проекции карточки отдают isoformat);
    непарсибельное значение возвращается как есть (честнее, чем прятать).
    """
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d.%m %H:%M")


def offset_label() -> str:
    """Метка зоны для UI: UTC, UTC+4, UTC+5:30, UTC-3…"""
    minutes = _offset_minutes()
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rem = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rem:02d}" if rem else "")
