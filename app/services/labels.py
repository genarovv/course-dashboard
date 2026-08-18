"""D8: русские ярлыки статусов/причин/уверенности для UI (решения CEO 2026-07-30).

Один факт — в одном месте: словари отсюда используют шаблоны (через globals
Jinja в app/routes) и сервисы-проекции; сырые значения enum остаются в данных
и CSS-классах, наружу к преподавателю уходят только русские подписи.
"""

from app.models import ArtifactRole, SnapshotStatus

# D7/D37: русские имена ролей — колонки матрицы и все экраны (один факт в одном месте)
ROLE_TITLES: dict[ArtifactRole, str] = {
    ArtifactRole.interview: "Интервью",
    ArtifactRole.persona: "Персоны",
    ArtifactRole.user_story: "User stories",
    ArtifactRole.prd: "PRD",
    ArtifactRole.data_model: "Схема данных",
    ArtifactRole.architecture: "Архитектура",
    ArtifactRole.plan: "План",
    ArtifactRole.code: "Код",
    ArtifactRole.tests: "Тесты",
    ArtifactRole.jtbd: "JTBD",
    ArtifactRole.readme: "README",
    ArtifactRole.changelog: "CHANGELOG",
    ArtifactRole.adr: "ADR",
    ArtifactRole.claude_md: "CLAUDE.md",
    ArtifactRole.roles_roster: "Ростер ролей",
}


# D38: подсказки заголовков колонок — расшифровка для преподавателя без жаргона
ROLE_HINTS: dict[ArtifactRole, str] = {
    ArtifactRole.interview: "записи проблемных интервью",
    ArtifactRole.persona: "портреты пользователей",
    ArtifactRole.user_story: "пользовательские истории",
    ArtifactRole.prd: "PRD — описание продукта и требований",
    ArtifactRole.data_model: "схема данных проекта",
    ArtifactRole.architecture: "архитектура решения",
    ArtifactRole.plan: "план разработки",
    ArtifactRole.code: "код проекта",
    ArtifactRole.tests: "автотесты",
    ArtifactRole.jtbd: "JTBD — задачи пользователя",
    ArtifactRole.readme: "README — обзор проекта",
    ArtifactRole.changelog: "CHANGELOG — журнал изменений",
    ArtifactRole.adr: "ADR — журнал архитектурных решений",
    ArtifactRole.claude_md: "CLAUDE.md — правила проекта для ИИ-агентов",
    ArtifactRole.roles_roster: "состав ролей проекта",
}

# D39: статусы merge request по-русски (слаг остаётся в CSS-классах)
MR_STATE_LABELS: dict[str, str] = {
    "opened": "открыт",
    "merged": "влит",
    "closed": "закрыт",
}


def role_title(role) -> str:
    """D37: подпись роли для человека; неизвестная роль деградирует до слага."""
    try:
        return ROLE_TITLES.get(ArtifactRole(role), str(role))
    except ValueError:
        return str(role)

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

# FR-14 этап 1 (#80): исход проверки приёма — наблюдение, не оценка (BR-2)
PRACTICE_STATUS_LABELS: dict[str, str] = {
    "passed": "применён",
    "failed": "не применён",
    "no_data": "нет данных",
}


def repo_short_name(repo_url: str) -> str:
    """Хвост адреса репозитория для строки матрицы; полный URL — в title."""
    return repo_url.rstrip("/").rsplit("/", 1)[-1] or repo_url
