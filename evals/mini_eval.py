# C3 (#34): мини-эвал deepseek-v4-flash — гейт Фазы 0 + ADR-004 Proposed→Accepted.
# Скрипт ВНЕ продукта (plan.md §4 волна 1): приложение его не импортирует; ядро FR-5
# (coherence_analyzer/llm_client) по-прежнему не кодится до снятия гейта — этот скрипт
# и есть механизм снятия. Контракт выхода и валидации повторяет §5.2 ARCHITECTURE,
# чтобы C1/C2 писались против уже проверенного формата.
#
# Запуск (из корня course-dashboard, ключ CD_DEEPSEEK_API_KEY в .env):
#   python -m evals.mini_eval fetch   # скачать тексты реальных пар в evals/pairs/ (в git не попадают)
#   python -m evals.mini_eval run     # прогнать 6 пар, написать отчёт в evals/
#   python -m evals.mini_eval all     # fetch + run
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAIRS_DIR = ROOT / "evals" / "pairs"


def report_date() -> str:
    from datetime import date

    return date.today().isoformat()

# Каркас рубрики v1 (evals/golden-set.md, 2026-07-02) + требование строгого JSON.
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
      "quote_a": "цитата A до 15 слов",
      "why_not": "что искал в B и почему не засчитал"}}],
  "notes": "до 3 строк"
}}
Целостность обязательна: entities_checked = entities_found + entities_excluded + entities_lost."""

# Правило по каркасу v1 для рёбер, у которых нет текста рубрики в app/config.yaml.
GENERIC_RULE = (
    "Проверка связности «{a} → {b}». B обязан опираться на A: каждая значимая сущность A "
    "либо отражена в B (синонимы засчитываются и называются), либо исключена явно "
    "(out-of-scope с обоснованием), либо потеряна. Разрыв = не менее одной потерянной "
    "значимой сущности."
)

# Тексты рубрик рёбер prd→data_model и data_model→architecture — из app/config.yaml (FR-2).
_CONFIG_RUBRICS: dict[str, str] | None = None


def config_rubric(edge: str) -> str:
    global _CONFIG_RUBRICS
    if _CONFIG_RUBRICS is None:
        import yaml

        cfg = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))
        _CONFIG_RUBRICS = {
            f"{e['source_role']}->{e['target_role']}": e["rubric"]["text"]
            for e in cfg["edges"]
        }
    return _CONFIG_RUBRICS[edge]


@dataclass(frozen=True)
class Pair:
    key: str
    title: str
    source_label: str  # человекочитаемый адрес A (репо + путь или локальный путь)
    target_label: str
    rubric_text: str
    etalon: str | None = None  # "ok" | "break" | None (реальные пары — эталон выносит CEO)
    etalon_note: str = ""
    # локальные файлы (golden set) — пути от корня проекта; удалённые — кеш в evals/pairs/
    source_local: str | None = None
    target_local: str | None = None


def _real(key: str, title: str, src: str, tgt: str, rubric: str) -> Pair:
    return Pair(
        key=key,
        title=title,
        source_label=src,
        target_label=tgt,
        rubric_text=rubric,
        source_local=None,
        target_local=None,
    )


PAIRS: list[Pair] = [
    Pair(
        key="GS-1",
        title="user stories преподавателя → PRD (эталон: НЕТ РАЗРЫВА)",
        source_label="product/user-stories/01-user-stories-преподаватель.md",
        target_label="product/prd.md",
        rubric_text=GENERIC_RULE.format(a="user stories", b="PRD"),
        etalon="ok",
        etalon_note="золотая точка: дрейф «слепой зоны» починен 2026-07-02 — её появление = ложная сработка",
        source_local="product/user-stories/01-user-stories-преподаватель.md",
        target_local="product/prd.md",
    ),
    Pair(
        key="GS-2",
        title="персона Оксана → user stories (эталон: РАЗРЫВ)",
        source_label="product/personas/04-преподаватель-оксана.md",
        target_label="product/user-stories/01-user-stories-преподаватель.md",
        rubric_text=GENERIC_RULE.format(a="персона", b="user stories"),
        etalon="break",
        etalon_note="ключевая точка: «Оксана потеряна молча»; прогон без этой точки — деградация",
        source_local="product/personas/04-преподаватель-оксана.md",
        target_local="product/user-stories/01-user-stories-преподаватель.md",
    ),
    _real(
        "R-1",
        "С-01: REQUIREMENTS → DATA_MODEL (рубрика prd→data_model v1.0)",
        "С-01:REQUIREMENTS.md",
        "С-01:DATA_MODEL.md",
        config_rubric("prd->data_model"),
    ),
    _real(
        "R-2",
        "С-01: DATA_MODEL → ARCHITECTURE (рубрика data_model→architecture v1.0)",
        "С-01:DATA_MODEL.md",
        "С-01:ARCHITECTURE.md",
        config_rubric("data_model->architecture"),
    ),
    _real(
        "R-3",
        "С-02: jtbd → PRD (каркас v1)",
        "С-02:product/jtbd.md",
        "С-02:product/prd.md",
        GENERIC_RULE.format(a="JTBD", b="PRD"),
    ),
    _real(
        "R-4",
        "С-02: PRD → ARCHITECTURE (каркас v1)",
        "С-02:product/prd.md",
        "С-02:ARCHITECTURE.md",
        GENERIC_RULE.format(a="PRD", b="архитектура"),
    ),
]

# Маппинг «код → адрес/ветка» деанонимизирует коды (URL содержат аккаунты студентов),
# поэтому в репозиторий не коммитится: живёт в неотслеживаемом evals/pairs/repos.json
# (каталог в .gitignore; канон маппинга — внутренний контур курса, не этот репо).
# Формат: {"С-01": {"url": "...", "git_host": "GitLab", "ref": "..."}, ...}
def load_repos() -> dict:
    path = PAIRS_DIR / "repos.json"
    if not path.exists():
        raise SystemExit(
            f"нет {path} — создай его локально по маппингу из внутреннего контура курса "
            "(digital-twin/ssot/course/студенты-маппинг-кодов.md)"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def build_prompt(rubric_text: str, source_text: str, target_text: str) -> str:
    return PROMPT_TEMPLATE.format(rubric=rubric_text, source=source_text, target=target_text)


REQUIRED_FIELDS = (
    "verdict",
    "confidence",
    "entities_checked",
    "entities_found",
    "entities_excluded",
    "entities_lost",
    "points",
)


def validate_response(raw: str) -> dict | None:
    """Schema-check по §5.2: обязательные поля, домены, целостность счётчиков, ≤5 точек."""
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
    # регистр нормализуем до проверки домена: "OK" от модели — не повод для deferred
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


@dataclass
class PairResult:
    pair: Pair
    verdict: str  # ok | break | deferred
    attempts: int
    reason: str | None = None  # parse_error | llm_unavailable
    data: dict | None = None
    etalon_match: bool | None = None  # None — эталона нет или deferred


async def run_pair(pair: Pair, source_text: str, target_text: str, call) -> PairResult:
    """Один прогон пары: 1 ретрай при невалидном ответе, затем deferred(parse_error) — §5.2."""
    prompt = build_prompt(pair.rubric_text, source_text, target_text)
    attempts = 0
    for _ in range(2):
        attempts += 1
        raw = await call(prompt)
        validated = validate_response(raw)
        if validated is not None:
            match = None if pair.etalon is None else validated["verdict"] == pair.etalon
            return PairResult(
                pair=pair,
                verdict=validated["verdict"],
                attempts=attempts,
                data=validated,
                etalon_match=match,
            )
    return PairResult(pair=pair, verdict="deferred", attempts=attempts, reason="parse_error")


def render_report(results: list[PairResult], llm_model: str) -> str:
    lines = [
        f"# Мини-эвал `{llm_model}` — {report_date()} (C3, #34)",
        "",
        "> Гейт Фазы 0 (PRD §13) + ADR-004 Proposed→Accepted. Скрипт: `evals/mini_eval.py`.",
        f"> `llm_model`: **{llm_model}** (обязательное поле прогона — канон Б1).",
        "> Эталоны есть только у GS-пар; вердикты реальных пар выносит на сверку CEO.",
        "",
        "| Пара | Что | Вердикт | Уверенность | Счётчики (checked=found+excl+lost) | Эталон | Совпал |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        d = r.data or {}
        counters = (
            f"{d.get('entities_checked', '—')}={d.get('entities_found', '—')}"
            f"+{d.get('entities_excluded', '—')}+{d.get('entities_lost', '—')}"
            if r.data
            else "—"
        )
        match = {True: "✅", False: "❌", None: "—"}[r.etalon_match]
        lines.append(
            f"| {r.pair.key} | {r.pair.title} | {r.verdict}"
            f"{f' ({r.reason})' if r.reason else ''} | {d.get('confidence', '—')} "
            f"| {counters} | {r.pair.etalon or '—'} | {match} |"
        )
    lines.append("")
    for r in results:
        if not r.data:
            continue
        lines.append(f"## {r.pair.key} — точки и заметки")
        lines.append(f"- A: `{r.pair.source_label}` → B: `{r.pair.target_label}`")
        for p in r.data.get("points", []):
            lines.append(
                f"- {p.get('entity', '?')} | «{p.get('quote_a', '')}» | {p.get('why_not', '')}"
            )
        notes = r.data.get("notes", "")
        if notes:
            lines.append(f"- Заметки: {notes}")
        if r.pair.etalon_note:
            lines.append(f"- Эталонная точка: {r.pair.etalon_note}")
        lines.append("")
    gs = [r for r in results if r.pair.etalon is not None]
    ok = sum(1 for r in gs if r.etalon_match)
    lines += [
        "## Итог для решения CEO по ADR-004",
        f"- Golden set: {ok}/{len(gs)} совпадений с эталоном.",
        "- Реальные пары: вердикты выше — на сверку преподавателем; подтверждённые пары — кандидаты в golden set.",
        "- Сходимость есть → ADR-004 Accepted, гейт Фазы 0 снят; нет → фиксируем расхождения и решаем по модели.",
        "",
    ]
    return "\n".join(lines)


# --- сеть: скачивание пар и вызов DeepSeek (в тестах не используется) ---


def _pair_cache_path(label: str) -> Path:
    return PAIRS_DIR / (label.replace("/", "__").replace(":", "--") + ".txt")


async def _head_sha(gc, repo: dict) -> str:
    """SHA головы ветки. Обход бага git_client: GitLab-ref со слэшами в path-сегменте
    commits/{ref} не кодируется (quote без safe='') → 404; фикс — отдельный тикет."""
    from urllib.parse import quote

    from app.clients.git_client import GitRepoUnavailableError, _parse_repo

    try:
        return await gc.get_head_sha(repo["url"], repo["git_host"], ref=repo["ref"])
    except GitRepoUnavailableError:
        if repo["git_host"] != "GitLab" or "/" not in repo["ref"]:
            raise
        host, path = _parse_repo(repo["url"])
        data = await gc._request_json(
            f"https://{host}/api/v4/projects/{quote(path, safe='')}"
            f"/repository/branches/{quote(repo['ref'], safe='')}",
            gc._gitlab_headers(),
        )
        return data["commit"]["id"]


async def fetch_pairs() -> None:
    """Скачать тексты реальных пар в evals/pairs/ (кеш; в git не коммитится — чужой контент)."""
    from app.clients.git_client import GitClient

    PAIRS_DIR.mkdir(parents=True, exist_ok=True)
    repos = load_repos()
    gc = GitClient()
    meta: dict[str, str] = {}
    try:
        for code, repo in repos.items():
            sha = await _head_sha(gc, repo)
            meta[code] = f"{repo['ref']} @ {sha}"
        labels = {
            lbl
            for p in PAIRS
            if p.source_local is None
            for lbl in (p.source_label, p.target_label)
        }
        for label in sorted(labels):
            code, path = label.split(":", 1)
            repo = repos[code]
            text = await gc.get_file_content(repo["url"], repo["git_host"], path, ref=repo["ref"])
            _pair_cache_path(label).write_text(text, encoding="utf-8")
            print(f"fetch {label}: {len(text)} символов")
    finally:
        await gc.aclose()
    (PAIRS_DIR / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("meta:", meta)


def _load_text(pair: Pair, which: str) -> str:
    local = pair.source_local if which == "a" else pair.target_local
    label = pair.source_label if which == "a" else pair.target_label
    if local is not None:
        return (ROOT / local).read_text(encoding="utf-8")
    cache = _pair_cache_path(label)
    if not cache.exists():
        raise SystemExit(f"нет кеша {cache.name} — сначала: python -m evals.mini_eval fetch")
    return cache.read_text(encoding="utf-8")


async def run_all() -> None:
    import httpx

    from app.config import settings

    if not settings.deepseek_api_key:
        raise SystemExit("CD_DEEPSEEK_API_KEY не задан в .env — предусловие P2 (plan.md §4)")
    model = settings.deepseek_model

    async with httpx.AsyncClient(timeout=180.0) as http:

        async def call(prompt: str) -> str:
            resp = await http.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        results = []
        for pair in PAIRS:
            print(f"прогон {pair.key}…")
            try:
                results.append(
                    await run_pair(pair, _load_text(pair, "a"), _load_text(pair, "b"), call)
                )
            except httpx.HTTPError as exc:
                # §5.2: недоступность LLM = deferred(llm_unavailable), прогон не теряем
                print(f"  {pair.key}: LLM недоступна ({exc})")
                results.append(
                    PairResult(pair=pair, verdict="deferred", attempts=1, reason="llm_unavailable")
                )

    report = render_report(results, llm_model=model)
    out = ROOT / "evals" / f"отчёт-мини-эвал-{report_date()}.md"
    out.write_text(report, encoding="utf-8")
    print(f"отчёт: {out}")
    for r in results:
        print(f"  {r.pair.key}: {r.verdict}" + (f" ({r.reason})" if r.reason else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Мини-эвал deepseek-v4-flash (C3, #34)")
    parser.add_argument("command", choices=["fetch", "run", "all"])
    args = parser.parse_args()
    if args.command in ("fetch", "all"):
        asyncio.run(fetch_pairs())
    if args.command in ("run", "all"):
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
