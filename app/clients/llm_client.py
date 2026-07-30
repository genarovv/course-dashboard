"""C1 (#35): llm_client — проверка связности пары артефактов через DeepSeek API (FR-5).

Контракт §5.2 ARCHITECTURE: строгий JSON, schema-check + целостность счётчиков,
1 повторный запрос при невалидном ответе (без эскалации промпта — решение v3),
затем None → вызывающий регистрирует deferred(parse_error). Сетевые/HTTP-ошибки →
LLMUnavailableError → deferred(llm_unavailable). Клиент не знает о моделях данных
(§3.2): работает с сырыми текстами и словарём-вердиктом.

Промпт-каркас — канон рубрики v1 (evals/golden-set.md); формат проверен
мини-эвалом 2026-07-30 (golden set 2/2, ADR-004 Accepted).
"""

import json
import re

import httpx

from app.config import settings


class LLMUnavailableError(Exception):
    """API недоступен (сеть/HTTP) → deferred(llm_unavailable), дашборд живёт (D26)."""


PROMPT_TEMPLATE = """Ты — агент-проверяющий связности учебных артефактов.
Работаешь строго по правилу ребра, ничего не додумываешь.

ПРАВИЛО РЕБРА:
{rubric}

АРТЕФАКТ A (источник):
<<<A
{source}
A>>>

АРТЕФАКТ B (цель, обязан опираться на A):
<<<B
{target}
B>>>

Задача: найди значимые сущности A и для каждой определи — отражена в B (синонимы
засчитываются и называются), исключена явно (out-of-scope с обоснованием)
или потеряна. Разрыв = не менее одной потерянной значимой сущности.

Ответь СТРОГО одним JSON-объектом без пояснений вокруг:
{{
  "verdict": "ok" | "break",
  "confidence": "high" | "medium" | "low",
  "entities_checked": <int>,
  "entities_found": <int>,
  "entities_excluded": <int>,
  "entities_lost": <int>,
  "points": [до 5 объектов {{"entity": "...",
      "quote": "цитата A до 15 слов",
      "why": "что искал в B и почему не засчитал"}}],
  "notes": "до 3 строк"
}}
Целостность обязательна: entities_checked = entities_found + entities_excluded + entities_lost."""

REQUIRED_FIELDS = (
    "verdict",
    "confidence",
    "entities_checked",
    "entities_found",
    "entities_excluded",
    "entities_lost",
    "points",
)


def build_prompt(rubric_text: str, source_text: str, target_text: str) -> str:
    return PROMPT_TEMPLATE.format(rubric=rubric_text, source=source_text, target=target_text)


def validate_llm_response(raw: str) -> dict | None:
    """Schema-check §5.2: обязательные поля, домены (регистр нормализуется),
    целостность счётчиков, ≤5 точек. Невалидно → None (решение о ретрае — у клиента)."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for fieldname in REQUIRED_FIELDS:
        if fieldname not in data:
            return None
    for fieldname in ("verdict", "confidence"):
        if isinstance(data[fieldname], str):
            data[fieldname] = data[fieldname].lower()
    if data["verdict"] not in ("ok", "break"):
        return None
    if data["confidence"] not in ("high", "medium", "low"):
        return None
    counters = [
        data["entities_checked"],
        data["entities_found"],
        data["entities_excluded"],
        data["entities_lost"],
    ]
    if not all(type(c) is int and c >= 0 for c in counters):
        return None
    if counters[0] != counters[1] + counters[2] + counters[3]:
        return None
    if not isinstance(data["points"], list) or len(data["points"]) > 5:
        return None
    return data


class LLMClient:
    """Async-клиент DeepSeek (ключ/адрес/модель — settings, NFR-3: секрет не в коде)."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=180.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def check_coherence(
        self, source_text: str, target_text: str, rubric_text: str, model: str | None = None
    ) -> dict | None:
        """Вердикт связности пары или None после 1 ретрая (→ deferred parse_error).

        temperature=0 + json_object — детерминизм прогона (канон Б1: вердикт
        воспроизводим по четвёрке). model — явная модель четвёрки (fix по ревью C2:
        вызывающий передаёт pair.llm_model, чтобы вердикт не приписался чужой модели);
        по умолчанию settings.deepseek_model.
        """
        prompt = build_prompt(rubric_text, source_text, target_text)
        for _ in range(2):
            try:
                response = await self._http.post(
                    f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json={
                        "model": model or settings.deepseek_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0,
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(str(exc)) from exc
            validated = validate_llm_response(content)
            if validated is not None:
                return validated
        return None
