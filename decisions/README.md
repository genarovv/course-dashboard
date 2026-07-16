# decisions/ — ADR проекта Course Dashboard

Формат — `../../project-context-template/decisions/0000-adr-template.md`.

Курсовые решения, на которые проект опирается (живут в SSOT курса, сюда не копируются):
- `../../digital-twin/ssot/course/decisions/ADR-003-model-sdachi-i-nablyudaemosti.md` — публичные репо, контракт артефактов `**/имя.md`, реестр через Яндекс.Форму → CSV. Это входные ограничения PRD (BR-5, BR-6).

Принятые ADR:
- `ADR-001-fr5a-llm-agent-po-pravilam.md` — ядро FR-5: LLM-агент по версионированным рубрикам (2026-07-02)

Принятые ADR (частично superseded ARCHITECTURE.md v2/v3):
- ADR-002 `ADR-002-stack-python-fastapi-sqlite.md` — стек: Python + FastAPI + SQLite (2026-07-07). Alpine.js удалён; у отвергнутых вариантов названы честные плюсы (2026-07-09).
- ADR-003 `ADR-003-component-architecture.md` — **superseded ARCHITECTURE.md v2+** (детали слоёв живут там; актуальная — v3 от 2026-07-09).

Предложенные ADR:
- ADR-004 `ADR-004-llm-model-deepseek-v4-flash.md` — LLM-модель ядра: `deepseek-v4-flash` (решение CEO 2026-07-09). **Proposed** → Accepted после мини-эвала (golden set + 2 реальных репо).
