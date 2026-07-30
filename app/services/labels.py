"""D8: русские ярлыки статусов/причин/уверенности для UI (решения CEO 2026-07-30).

Один факт — в одном месте: словари отсюда используют шаблоны (через globals
Jinja в app/routes) и сервисы-проекции; сырые значения enum остаются в данных
и CSS-классах, наружу к преподавателю уходят только русские подписи.
"""

from app.models import SnapshotStatus

STATUS_LABELS: dict[SnapshotStatus, str] = {
    SnapshotStatus.found: "есть",
    SnapshotStatus.partial: "частично",
    SnapshotStatus.not_found: "нет",
}

# BR-3: причины «частично» — словами, как в модалке артефактной матрицы (D7)
PARTIAL_LABELS: dict[str, str] = {
    "template_copy": "заготовка из шаблона",
    "inexact_name": "неточное имя файла",
    "wrong_place": "не в ожидаемом месте",
}

# FR-5: шкала уверенности «высокая / средняя / низкая» (PRD §5)
CONFIDENCE_LABELS: dict[str, str] = {
    "high": "высокая",
    "medium": "средняя",
    "low": "низкая",
}


def repo_short_name(repo_url: str) -> str:
    """Хвост адреса репозитория для строки матрицы; полный URL — в title."""
    return repo_url.rstrip("/").rsplit("/", 1)[-1] or repo_url
