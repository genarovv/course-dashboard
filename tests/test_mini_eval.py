# C3 (#34): тесты мини-эвала — состав пар, маппинг результатов, отчёт.
# Промпт, schema-check и ретрай канонически живут в app/clients/llm_client.py
# и покрыты tests/test_llm_client.py (C1/#35) — здесь не дублируются.
import asyncio

from evals.mini_eval import (
    PAIRS,
    LLMUnavailableError,
    Pair,
    render_report,
    run_pair,
)

VALID_VERDICT = {
    "verdict": "ok",
    "confidence": "high",
    "entities_checked": 5,
    "entities_found": 3,
    "entities_excluded": 2,
    "entities_lost": 0,
    "points": [],
    "notes": "все сущности отражены",
}


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


def test_config_rubrics_are_loaded_for_real_pairs():
    # R-1/R-2 обязаны использовать тексты рубрик из app/config.yaml, не дубликаты
    by_key = {p.key: p for p in PAIRS}
    assert "PRD → схема данных" in by_key["R-1"].rubric_text
    assert "схема данных → архитектура" in by_key["R-2"].rubric_text


# --- run_pair: маппинг результатов клиента (ретрай/валидация — в самом клиенте) ---


def _pair(etalon="ok"):
    return Pair(
        key="T-1",
        title="тестовая пара",
        source_label="A.md",
        target_label="B.md",
        rubric_text="правило",
        etalon=etalon,
    )


def test_run_pair_valid_verdict_and_etalon_match():
    async def check(source, target, rubric):
        assert rubric == "правило"  # рубрика пары доходит до клиента
        return dict(VALID_VERDICT)

    result = asyncio.run(run_pair(_pair(), "текст A", "текст B", check))
    assert result.verdict == "ok"
    assert result.etalon_match is True


def test_run_pair_none_from_client_is_deferred_parse_error():
    async def check(source, target, rubric):
        return None  # клиент исчерпал ретрай

    result = asyncio.run(run_pair(_pair(), "a", "b", check))
    assert result.verdict == "deferred"
    assert result.reason == "parse_error"
    assert result.etalon_match is None


def test_run_pair_llm_unavailable_is_deferred_without_crash():
    async def check(source, target, rubric):
        raise LLMUnavailableError("сеть")

    result = asyncio.run(run_pair(_pair(), "a", "b", check))
    assert result.verdict == "deferred"
    assert result.reason == "llm_unavailable"


def test_run_pair_etalon_mismatch_is_flagged():
    async def check(source, target, rubric):
        data = dict(VALID_VERDICT)
        data.update(verdict="break", entities_found=0, entities_excluded=0, entities_lost=5)
        return data

    result = asyncio.run(run_pair(_pair(), "a", "b", check))
    assert result.verdict == "break"
    assert result.etalon_match is False


# --- отчёт ---


def test_report_fixes_llm_model_and_lists_all_pairs():
    async def check(source, target, rubric):
        return dict(VALID_VERDICT)

    async def collect():
        return [await run_pair(p, "a", "b", check) for p in PAIRS[:2]]

    results = asyncio.run(collect())
    report = render_report(results, llm_model="deepseek-v4-flash")
    # llm_model — обязательное поле прогона (канон Б1, golden-set.md)
    assert "deepseek-v4-flash" in report
    for r in results:
        assert r.pair.key in report
