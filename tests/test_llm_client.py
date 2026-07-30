# C1 (#35): llm_client — DeepSeek API на httpx.MockTransport (без сети).
# Контракт §5.2: строгий JSON, schema-check, целостность счётчиков,
# 1 ретрай при невалидном ответе → None (deferred parse_error у вызывающего),
# сетевые/HTTP-ошибки → LLMUnavailableError (deferred llm_unavailable).
import asyncio
import json

import httpx

from app.clients.llm_client import (
    LLMClient,
    LLMUnavailableError,
    build_prompt,
    validate_llm_response,
)
from app.config import settings

VALID_RESPONSE = {
    "verdict": "ok",
    "confidence": "high",
    "entities_checked": 4,
    "entities_found": 3,
    "entities_excluded": 1,
    "entities_lost": 0,
    "points": [],
    "notes": "",
}


def make_content(**overrides):
    data = dict(VALID_RESPONSE)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _api_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _client(handler) -> LLMClient:
    return LLMClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _run(coro):
    return asyncio.run(coro)


# --- check_coherence ---


def test_check_coherence_valid_first_try():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/completions")
        assert payload["model"] == settings.deepseek_model  # канон Б1: модель из settings
        assert payload["temperature"] == 0.0  # детерминизм прогона
        return _api_response(make_content())

    result = _run(_client(handler).check_coherence("текст A", "текст B", "правило ребра"))
    assert result is not None
    assert result["verdict"] == "ok"
    assert len(requests) == 1


def test_check_coherence_retries_once_then_valid():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _api_response("мусор" if len(calls) == 1 else make_content())

    result = _run(_client(handler).check_coherence("a", "b", "r"))
    assert result is not None
    assert len(calls) == 2


def test_check_coherence_double_invalid_returns_none():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _api_response("не json")

    result = _run(_client(handler).check_coherence("a", "b", "r"))
    assert result is None  # вызывающий регистрирует deferred(parse_error)
    assert len(calls) == 2  # ровно 1 ретрай, не 3 (решение v3: без эскалации промпта)


def test_check_coherence_http_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    try:
        _run(_client(handler).check_coherence("a", "b", "r"))
        raise AssertionError("ожидался LLMUnavailableError")
    except LLMUnavailableError:
        pass


def test_check_coherence_network_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    try:
        _run(_client(handler).check_coherence("a", "b", "r"))
        raise AssertionError("ожидался LLMUnavailableError")
    except LLMUnavailableError:
        pass


# --- промпт ---


def test_build_prompt_contains_rubric_texts_and_contract_fields():
    prompt = build_prompt("ПРАВИЛО", "ИСТОЧНИК-A", "ЦЕЛЬ-B")
    assert "ПРАВИЛО" in prompt
    assert "ИСТОЧНИК-A" in prompt
    assert "ЦЕЛЬ-B" in prompt
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


# --- validate_llm_response (schema-check §5.2) ---


def test_validate_accepts_valid_and_normalizes_case():
    validated = validate_llm_response(make_content(verdict="OK", confidence="High"))
    assert validated is not None
    assert validated["verdict"] == "ok"
    assert validated["confidence"] == "high"


def test_validate_accepts_json_in_code_fence():
    assert validate_llm_response("```json\n" + make_content() + "\n```") is not None


def test_validate_rejects_counter_mismatch():
    assert validate_llm_response(make_content(entities_checked=99)) is None


def test_validate_rejects_more_than_five_points():
    points = [{"entity": f"e{i}", "quote_a": "q", "why_not": "w"} for i in range(6)]
    assert (
        validate_llm_response(
            make_content(
                verdict="break",
                entities_found=0,
                entities_excluded=0,
                entities_lost=4,
                points=points,
            )
        )
        is None
    )


def test_validate_rejects_missing_field_and_bad_domain():
    data = dict(VALID_RESPONSE)
    del data["confidence"]
    assert validate_llm_response(json.dumps(data)) is None
    assert validate_llm_response(make_content(verdict="maybe")) is None
