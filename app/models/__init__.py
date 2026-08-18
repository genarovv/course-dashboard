from enum import StrEnum

from sqlalchemy import types
from sqlalchemy.orm import DeclarativeBase


class ArtifactRole(StrEnum):
    interview = "interview"
    persona = "persona"
    user_story = "user_story"
    prd = "prd"
    data_model = "data_model"
    architecture = "architecture"
    plan = "plan"
    code = "code"
    tests = "tests"
    # T3 (#43) + пакет «12 артефактов» (решение CEO 2026-07-30): роли для
    # артефактов 1, 2, 7, 9 описания курса; enum хранится строкой — без миграции
    jtbd = "jtbd"
    readme = "readme"
    changelog = "changelog"
    adr = "adr"
    claude_md = "claude_md"
    roles_roster = "roles_roster"


class GitHost(StrEnum):
    GitLab = "GitLab"
    GitHub = "GitHub"


class SnapshotStatus(StrEnum):
    found = "found"
    partial = "partial"
    not_found = "not_found"


# Ранжирование статусов для альтернативных путей одной роли (пакет «12 артефактов»,
# решение CEO 2026-07-30): лучший представитель роли в матрице и карточке.
# Живёт рядом с доменом, а не в сервисе — иначе циркулярный импорт
# matrix_builder ↔ evidence_chain (находка 9 ревью T3).
SNAPSHOT_STATUS_RANK = {
    SnapshotStatus.found: 0,
    SnapshotStatus.partial: 1,
    SnapshotStatus.not_found: 2,
}


class PartialReason(StrEnum):
    inexact_name = "inexact_name"
    wrong_place = "wrong_place"
    template_copy = "template_copy"


class SyncTrigger(StrEnum):
    schedule = "schedule"
    manual = "manual"


class SyncStatus(StrEnum):
    in_progress = "in_progress"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class SyncOutcome(StrEnum):
    ok_changed = "ok_changed"
    ok_unchanged = "ok_unchanged"
    repo_unavailable = "repo_unavailable"
    auth_failed = "auth_failed"
    skipped_rate_limit = "skipped_rate_limit"


class PracticeStatus(StrEnum):
    """FR-14 этап 1 (#80): исход проверки приёма курса — наблюдение, не оценка (BR-2)."""

    passed = "passed"
    failed = "failed"
    no_data = "no_data"


class RubricType(StrEnum):
    edge = "edge"
    step = "step"


class VerdictValue(StrEnum):
    ok = "ok"
    break_ = "break"
    deferred = "deferred"


class DeferredReason(StrEnum):
    llm_unavailable = "llm_unavailable"
    parse_error = "parse_error"


class ConfidenceLevel(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class CardStatus(StrEnum):
    done = "done"
    deferred = "deferred"


class EnumColumn(types.TypeDecorator):
    impl = types.String
    cache_ok = True

    def __init__(self, enum_type):
        self.enum_type = enum_type
        super().__init__()

    def process_bind_param(self, value, dialect):
        return value.value if isinstance(value, self.enum_type) else value

    def process_result_value(self, value, dialect):
        return self.enum_type(value) if value else None


class Base(DeclarativeBase):
    pass
