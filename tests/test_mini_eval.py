# C3 (#34): тесты мини-эвала deepseek-v4-flash — скрипт вне продукта (evals/mini_eval.py).
# Сеть не трогаем: LLM-вызов инъецируется, проверяем промпт, валидацию ответа,
# ретрай→deferred и отчёт (llm_model — обязательное поле, канон Б1).
import asyncio
import json

from evals.mini_eval import (
    PAIRS,
    Pair,
    build_prompt,
    render_report,
    run_pair,
    validate_response,
)

VALID_RESPONSE = {
    "verdict": "ok",
    "confidence": "high",
    "entities_checked": 5,
    "entities_found": 3,
    "entities_excluded": 2,
    "entities_lost": 0,
    "points": [],
    "notes": "все сущности отражены",
}


def make_raw(**overrides):
    data = dict(VALID_RESPONSE)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# --- состав пар ---


def test_pairs_composition():
    # GS-1/GS-2 с эталонами + 4 реальные пары (С-01 ×2, С-02 ×2) без эталонов
    assert len(PAIRS) == 6
    gs = [p for p in PAIRS if p.etalon is not None]
    real = [p for p in PAIRS if p.etalon is None]
    assert {p.key for p in gs} == {"GS-1", "GS-2"}
    assert {p.etalon for p in gs} == {"ok", "break"}
    assert len(real) == 4
    for p in PAIRS:
        assert p.rubric_text.strip(), f"пустая рубрика у {p.key}"


# --- промпт ---


def test_build_prompt_contains_rubric_and_both_texts():
    prompt = build_prompt("ПРАВИЛО-РЕБРА", "ТЕКСТ-ИСТОЧНИКА-A", "ТЕКСТ-ЦЕЛИ-B")
    assert "ПРАВИЛО-РЕБРА" in prompt
    assert "ТЕКСТ-ИСТОЧНИКА-A" in prompt
    assert "ТЕКСТ-ЦЕЛИ-B" in prompt
    # контракт §5.2: строгий JSON со всеми обязательными полями
    for fieldname in (
        "verdict",
        "confidence",
        "entities_checked",
        "entities_found",
        "entities_excluded",
        "entities_lost",
        "points",
    ):
        assert fieldname in prompt


# --- validate_response: schema-check §5.2 ---


def test_validate_accepts_valid_response():
    validated = validate_response(make_raw())
    assert validated is not None
    assert validated["verdict"] == "ok"


def test_validate_accepts_json_in_code_fence():
    raw = "```json\n" + make_raw() + "\n```"
    assert validate_response(raw) is not None


def test_validate_rejects_non_json():
    assert validate_response("НЕТ РАЗРЫВА, уверенность высокая") is None


def test_validate_rejects_missing_field():
    data = dict(VALID_RESPONSE)
    del data["confidence"]
    assert validate_response(json.dumps(data)) is None


def test_validate_rejects_unknown_verdict():
    assert validate_response(make_raw(verdict="maybe")) is None


def test_validate_rejects_unknown_confidence():
    assert validate_response(make_raw(confidence="超高")) is None


def test_validate_rejects_counter_mismatch():
    # целостность: checked == found + excluded + lost
    assert validate_response(make_raw(entities_checked=99)) is None


def test_validate_normalizes_uppercase_verdict():
    # "OK" от модели — не повод для deferred (ретрай тем же промптом при t=0 бессмыслен)
    validated = validate_response(make_raw(verdict="OK", confidence="High"))
    assert validated is not None
    assert validated["verdict"] == "ok"
    assert validated["confidence"] == "high"


def test_validate_rejects_negative_counters():
    assert (
        validate_response(
            make_raw(entities_checked=1, entities_found=2, entities_excluded=0, entities_lost=-1)
        )
        is None
    )


def test_validate_rejects_bool_counters():
    # bool проходит isinstance(int) — защищаемся от True/False в счётчиках
    assert (
        validate_response(
            make_raw(entities_checked=2, entities_found=True, entities_excluded=1, entities_lost=0)
        )
        is None
    )


def test_validate_rejects_points_not_a_list():
    assert validate_response(make_raw(points="нет точек")) is None


def test_config_rubrics_are_loaded_for_real_pairs():
    # R-1/R-2 обязаны использовать тексты рубрик из app/config.yaml, не дубликаты
    by_key = {p.key: p for p in PAIRS}
    assert "PRD → схема данных" in by_key["R-1"].rubric_text
    assert "схема данных → архитектура" in by_key["R-2"].rubric_text


def test_validate_rejects_more_than_five_points():
    points = [
        {"entity": f"e{i}", "quote_a": "q", "why_not": "w"} for i in range(6)
    ]
    assert (
        validate_response(
            make_raw(
                verdict="break",
                entities_found=0,
                entities_excluded=0,
                entities_lost=5,
                points=points,
            )
        )
        is None
    )


# --- run_pair: 1 ретрай → deferred(parse_error), §5.2 ---


def _pair():
    return Pair(
        key="T-1",
        title="тестовая пара",
        source_label="A.md",
        target_label="B.md",
        rubric_text="правило",
        etalon="ok",
    )


def test_run_pair_ok_first_try():
    async def call(prompt):
        return make_raw()

    result = asyncio.run(run_pair(_pair(), "текст A", "текст B", call))
    assert result.verdict == "ok"
    assert result.attempts == 1
    assert result.etalon_match is True


def test_run_pair_retries_once_then_ok():
    calls = []

    async def call(prompt):
        calls.append(prompt)
        return "мусор" if len(calls) == 1 else make_raw()

    result = asyncio.run(run_pair(_pair(), "a", "b", call))
    assert result.verdict == "ok"
    assert result.attempts == 2


def test_run_pair_double_failure_is_deferred():
    async def call(prompt):
        return "не json"

    result = asyncio.run(run_pair(_pair(), "a", "b", call))
    assert result.verdict == "deferred"
    assert result.reason == "parse_error"
    assert result.etalon_match is None


def test_run_pair_etalon_mismatch_is_flagged():
    async def call(prompt):
        return make_raw(
            verdict="break",
            entities_found=0,
            entities_excluded=0,
            entities_lost=5,
        )

    result = asyncio.run(run_pair(_pair(), "a", "b", call))
    assert result.verdict == "break"
    assert result.etalon_match is False


# --- отчёт ---


def test_report_fixes_llm_model_and_lists_all_pairs():
    async def call(prompt):
        return make_raw()

    async def collect():
        return [await run_pair(p, "a", "b", call) for p in PAIRS[:2]]

    results = asyncio.run(collect())
    report = render_report(results, llm_model="deepseek-v4-flash")
    # llm_model — обязательное поле прогона (канон Б1, golden-set.md)
    assert "deepseek-v4-flash" in report
    for r in results:
        assert r.pair.key in report
