"""FR-11-пилот (#45): рубрика КАЧЕСТВА одного шага (README) скриптом вне продукта.

Решения CEO 2026-07-09/28: FR-11 в продукт v1 не строим; пилот — скрипт по образцу
мини-эвала, модель зафиксирована, результат = материал занятия 15 + данные
точности для решения о FR-11 в v2. Это оценка КАЧЕСТВА документа (не связности
пары) — свой контракт выхода: verdict + критерии с пометками met/не met.
"""

import json

from evals.fr11_pilot import (
    README_RUBRIC,
    RUBRIC_VERSION,
    build_prompt,
    render_report,
    validate_response,
)

VALID = {
    "verdict": "с оговорками",
    "criteria": [
        {"key": "purpose", "met": True, "note": "назначение ясно из первого абзаца"},
        {"key": "run", "met": False, "note": "команд запуска нет"},
    ],
    "notes": "коротко и честно",
}


def make_raw(**overrides):
    data = dict(VALID)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# ── рубрика ─────────────────────────────────────────────────────────────────


def test_rubric_declares_version_and_criteria_keys():
    assert RUBRIC_VERSION == "1.0"
    # 5 объявленных критериев качества README — каждый упомянут в тексте рубрики
    for key in ("purpose", "run", "structure", "status", "honesty"):
        assert key in README_RUBRIC, f"критерий {key} не описан в рубрике"


def test_prompt_contains_rubric_and_document():
    prompt = build_prompt("ТЕКСТ-README")
    assert "ТЕКСТ-README" in prompt
    assert "verdict" in prompt and "criteria" in prompt
    assert "purpose" in prompt  # ключи критериев доходят до модели


# ── валидация ответа ────────────────────────────────────────────────────────


def test_validate_accepts_valid():
    validated = validate_response(make_raw())
    assert validated is not None
    assert validated["verdict"] == "с оговорками"


def test_validate_rejects_unknown_verdict():
    assert validate_response(make_raw(verdict="шедевр")) is None


def test_validate_rejects_criteria_without_key_or_met():
    assert validate_response(make_raw(criteria=[{"met": True}])) is None
    assert validate_response(make_raw(criteria=[{"key": "purpose"}])) is None
    assert validate_response(make_raw(criteria="не список")) is None


def test_validate_rejects_non_bool_met():
    assert validate_response(make_raw(criteria=[{"key": "purpose", "met": "да"}])) is None


def test_validate_rejects_garbage():
    assert validate_response("готово, всё отлично!") is None


# ── отчёт ───────────────────────────────────────────────────────────────────


def test_report_fixes_model_and_rubric_version():
    rows = [
        {"repo_label": "С-01", "verdict": "годно", "criteria": VALID["criteria"], "notes": ""},
        {"repo_label": "С-02", "verdict": None, "criteria": [], "notes": None},  # README нет
    ]
    report = render_report(rows, llm_model="deepseek-v4-flash")
    assert "deepseek-v4-flash" in report  # канон Б1: модель зафиксирована
    assert RUBRIC_VERSION in report
    assert "С-01" in report and "С-02" in report
    assert "README не найден" in report
