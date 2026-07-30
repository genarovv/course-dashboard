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

FIVE = [
    {"key": "purpose", "met": True, "note": "назначение ясно"},
    {"key": "run", "met": False, "note": "команд запуска нет"},
    {"key": "structure", "met": True, "note": "карта файлов есть"},
    {"key": "status", "met": True, "note": "стадия описана"},
    {"key": "honesty", "met": True, "note": "заглушек нет"},
]

VALID = {
    "verdict": "с оговорками",
    "criteria": FIVE,
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
    assert validate_response(make_raw(criteria=[{"met": True}] + FIVE[1:])) is None
    assert validate_response(make_raw(criteria=[{"key": "purpose"}] + FIVE[1:])) is None
    assert validate_response(make_raw(criteria="не список")) is None


def test_validate_requires_exactly_five_unique_domain_keys():
    """Fix по ревью (находка 4): ровно 5 критериев из домена, без дублей."""
    assert validate_response(make_raw(criteria=FIVE[:4])) is None  # четыре
    assert validate_response(make_raw(criteria=FIVE + [FIVE[0]])) is None  # шесть
    dup = [dict(c) for c in FIVE]
    dup[1]["key"] = "purpose"  # дубль ключа
    assert validate_response(make_raw(criteria=dup)) is None
    alien = [dict(c) for c in FIVE]
    alien[4]["key"] = "beauty"  # чужой ключ
    assert validate_response(make_raw(criteria=alien)) is None


def test_rule_verdict_computed_in_code_not_by_model():
    """Fix по ревью (находка 1): вердикт считается кодом из met-счёта;
    слово модели — только для сверки (расхождение помечается)."""
    from evals.fr11_pilot import rule_verdict

    assert rule_verdict(FIVE) == "с оговорками"  # 4 met
    all_met = [dict(c, met=True) for c in FIVE]
    assert rule_verdict(all_met) == "годно"
    two_met = [dict(c, met=(c["key"] in ("purpose", "run"))) for c in FIVE]
    assert rule_verdict(two_met) == "негодно"


def test_validate_rejects_non_bool_met():
    assert validate_response(make_raw(criteria=[{"key": "purpose", "met": "да"}])) is None


def test_validate_rejects_garbage():
    assert validate_response("готово, всё отлично!") is None


# ── отчёт ───────────────────────────────────────────────────────────────────


def test_report_fixes_model_and_rubric_version():
    rows = [
        {"repo_label": "р-01", "verdict": "годно", "model_verdict": "годно",
         "criteria": [dict(c, met=True) for c in FIVE], "notes": ""},
        {"repo_label": "р-02", "verdict": None, "reason": "no_readme",
         "criteria": [], "notes": None},
    ]
    report = render_report(rows, llm_model="deepseek-v4-flash")
    assert "deepseek-v4-flash" in report  # канон Б1: модель зафиксирована
    assert RUBRIC_VERSION in report
    assert "р-01" in report and "р-02" in report
    assert "README не найден" in report


def test_report_distinguishes_none_reasons_and_flags_mismatch():
    """Fix по ревью (находки 1–3): причины «нет вердикта» различимы,
    расхождение вердикта модели с правилом — помечено."""
    rows = [
        {"repo_label": "р-01", "verdict": None, "reason": "llm_unavailable",
         "criteria": [], "notes": None},
        {"repo_label": "р-02", "verdict": None, "reason": "parse_error",
         "criteria": [], "notes": None},
        {"repo_label": "р-03", "verdict": "негодно", "model_verdict": "годно",
         "criteria": [dict(c, met=(c["key"] in ("purpose", "run"))) for c in FIVE],
         "notes": ""},
    ]
    report = render_report(rows, llm_model="m")
    assert "LLM недоступна" in report
    assert "не распарсился" in report
    assert "негодно" in report and "расхождение" in report  # правило кода победило
