"""#80 (FR-14 этап 1): конфиг проверок приёмов — AC 10.

Секция practice_checks в config.yaml + усиленные process_markers + новые
content_probes работают через СУЩЕСТВУЮЩИЕ механизмы (ноль кода в пробах и
маркерах): тест читает боевой config.yaml и проверяет, что ключи присутствуют,
а regex-паттерны валидны и ловят то, ради чего написаны.
"""

import re

import pytest

from app.services.config_manager import PracticeCheckConfig, load_config

EXPECTED_CHECK_KEYS = {
    "tests_first", "bug_repro", "docs_sync", "ticket_id_share", "review_round",
    "public_url", "env_example", "reviewer_config", "machine_lock", "deps_pinned",
}


@pytest.fixture(scope="module")
def config():
    return load_config()


def test_practice_checks_keys_present(config):
    assert config.practice_checks is not None
    assert {c.key for c in config.practice_checks} == EXPECTED_CHECK_KEYS


def test_practice_checks_patterns_compile(config):
    for check in config.practice_checks:
        if check.pattern:
            re.compile(check.pattern)
        if check.waiver_pattern:
            re.compile(check.waiver_pattern)


def test_practice_checks_kinds_have_required_fields(config):
    """Fail-fast конфига: у каждого вида — свои обязательные поля (валидатор Pydantic)."""
    by_key = {c.key: c for c in config.practice_checks}
    assert by_key["tests_first"].kind == "mr_commit_pattern" and by_key["tests_first"].pattern
    assert by_key["bug_repro"].kind == "mr_commit_pattern" and by_key["bug_repro"].pattern
    assert by_key["docs_sync"].kind == "mr_docs_sync" and by_key["docs_sync"].waiver_pattern
    assert by_key["ticket_id_share"].kind == "commit_id_share"
    assert by_key["ticket_id_share"].threshold == 0.5
    assert by_key["review_round"].kind == "review_round"
    assert by_key["public_url"].kind == "readme_url"
    for key in ("env_example", "reviewer_config", "machine_lock", "deps_pinned"):
        assert by_key[key].kind == "tree_probe" and by_key[key].tree_patterns, key


def test_practice_check_validator_rejects_incomplete():
    with pytest.raises(ValueError):
        PracticeCheckConfig(key="x", lesson=1, label="x", kind="mr_commit_pattern")
    with pytest.raises(ValueError):
        PracticeCheckConfig(key="x", lesson=1, label="x", kind="tree_probe")


def test_ticket_pattern_matches_course_conventions(config):
    """Паттерн ID тикета ловит конвенции потока: #12, ABC-7, T80/T-005."""
    pattern = {c.key: c for c in config.practice_checks}["ticket_id_share"].pattern
    for message in ("#12 fix", "PROJ-7: шаг", "T80: tests first", "T-005: слой данных"):
        assert re.search(pattern, message), message
    assert not re.search(pattern, "просто правки")


# ── process_markers: усиленные маркеры недель (существующий механизм FR-12) ──


def test_process_markers_mutation_and_cause_evidence(config):
    markers = {m.key: m for m in config.process_markers}
    assert {"mutation", "cause", "evidence"} <= set(markers)

    # мутация зачитывается только вместе с пойманным тестом
    assert re.search(markers["mutation"].pattern, "Мутация: убрал проверку. Поймал: test_login")
    assert not re.search(markers["mutation"].pattern, "Мутация: убрал проверку.")

    # причина и доказательство обязаны быть непустыми
    assert re.search(markers["cause"].pattern, "Причина: гонка транзакций")
    assert not re.search(markers["cause"].pattern, "Причина:")
    assert re.search(markers["evidence"].pattern, "Доказательство: тест красный до фикса")
    assert not re.search(markers["evidence"].pattern, "Доказательство:")


def test_process_markers_docs_waiver(config):
    """Waiver для docs_sync попадает в MrObservation.markers существующим механизмом."""
    markers = {m.key: m for m in config.process_markers}
    assert "docs_waiver" in markers
    assert re.search(
        markers["docs_waiver"].pattern,
        "Докам обновление не требуется, потому что интерфейс не менялся",
    )


# ── content_probes: jtbd / data_model / prd (существующий механизм T2 #44) ──
# В боевой config.yaml пробы НЕ включены: контрактный тест test_content_probes
# (узкая редакция T2) разрешает пробы только claude_md и architecture; включение =
# изменение того теста = изменение требования, утверждает CEO при merge. Здесь
# проверяется сам механизм: пробы спеки парсятся конфиг-схемой и ловят то,
# ради чего написаны (в config.yaml они лежат подготовленным комментарием).

SPEC_PROBES_YAML = """
lessons:
  - number: 4
    title: "JTBD"
    date: 2026-06-25
    artifacts:
      - role: jtbd
        expected_pattern: "product/jtbd.md"
        content_probes:
          - key: jtbd_three_part
            label: "трёхчастная формула JTBD"
            contains: "Когда.*(хочу|нужно).*чтобы"
  - number: 6
    title: "Схема данных"
    date: 2026-07-02
    artifacts:
      - role: data_model
        expected_pattern: "data-model.md"
        content_probes:
          - key: erdiagram
            label: "ER-диаграмма mermaid"
            contains: "erDiagram"
  - number: 5
    title: "PRD"
    date: 2026-06-30
    artifacts:
      - role: prd
        expected_pattern: "product/prd.md"
        content_probes:
          - key: acceptance_criteria
            label: "критерии приёмки"
            contains: "Given|Когда.*Тогда|- \\\\[ \\\\]"
"""


def _probes_by_role(config):
    probes: dict[str, list] = {}
    for lesson in config.lessons:
        for artifact in lesson.artifacts:
            if artifact.content_probes:
                probes.setdefault(str(artifact.role), []).extend(artifact.content_probes)
    return probes


@pytest.fixture(scope="module")
def spec_probes():
    from app.services.config_manager import parse_config

    return _probes_by_role(parse_config(SPEC_PROBES_YAML))


def test_probe_jtbd_three_part(spec_probes):
    (probe,) = [p for p in spec_probes["jtbd"] if p.contains]
    three_part = "Когда я готовлюсь к занятию, я хочу видеть матрицу, чтобы не листать репозитории"
    assert re.search(probe.contains, three_part, re.I | re.M)
    assert not re.search(probe.contains, "Просто список пожеланий", re.I | re.M)


def test_probe_data_model_erdiagram(spec_probes):
    probes = spec_probes["data_model"]
    assert probes, "у роли data_model обязана быть проба erDiagram"
    assert any(p.contains and re.search(p.contains, "```mermaid\nerDiagram\n```") for p in probes)


def test_probe_prd_acceptance_criteria(spec_probes):
    matching = [p for p in spec_probes["prd"] if p.contains]
    assert matching, "у роли prd обязана быть проба критериев приёмки"
    pattern = matching[0].contains
    for sample in ("Given пустой реестр", "Когда обход завершён, Тогда метка обновится", "- [ ] критерий"):
        assert re.search(pattern, sample, re.I | re.M), sample
