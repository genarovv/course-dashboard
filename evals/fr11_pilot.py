# FR-11-пилот (#45): рубрика КАЧЕСТВА одного шага — README — скриптом вне продукта.
# Решения CEO 2026-07-09/28: FR-11 в продукт v1 не строим; пилот даёт материал
# занятия 15 и данные точности для решения о FR-11 в v2. Оценки студентов —
# в локальный отчёт (в git не коммитится: цитаты из репозиториев).
#
# Запуск (из корня, ключ DeepSeek в .env):  python -m evals.fr11_pilot
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUBRIC_VERSION = "1.0"

# Драфт агента, рубрика утверждена решением CEO 2026-07-31 (прецедент #42).
# Критерии — из материала занятия 10 «Управление документацией»: README отвечает
# читателю-новичку, а не автору.
README_RUBRIC = """Рубрика качества README v1.0. Оцени документ по 5 критериям:
1. purpose — назначение: из первых абзацев понятно, ЧТО это за проект и для кого.
2. run — запуск: есть воспроизводимые шаги запуска с нуля (команды, зависимости);
   ссылки «смотри другой файл» засчитываются, если путь назван.
3. structure — навигация: описана структура проекта или дана карта ключевых файлов.
4. status — статус: сказано, что уже работает, а что нет (стадия проекта).
5. honesty — честность: README не обещает того, чего явно нет в тексте самого
   документа (заглушки «TODO», нетронутые шаблонные фразы = не met).
Критерий met только при явном подтверждении текстом. Суди ТОЛЬКО по тексту README."""

VERDICTS = ("годно", "с оговорками", "негодно")
CRITERIA_KEYS = ("purpose", "run", "structure", "status", "honesty")

PROMPT_TEMPLATE = """Ты — методист курса, оцениваешь КАЧЕСТВО README студенческого проекта строго по рубрике.

РУБРИКА:
{rubric}

ДОКУМЕНТ README:
<<<DOC
{document}
DOC>>>

Ответь СТРОГО одним JSON-объектом без пояснений вокруг:
{{
  "verdict": "годно" | "с оговорками" | "негодно",
  "criteria": [ровно 5 объектов {{"key": "purpose|run|structure|status|honesty",
      "met": true|false,
      "note": "короткое обоснование с опорой на текст"}}],
  "notes": "до 3 строк общего впечатления"
}}
Правило вердикта: все 5 met — «годно»; 3–4 met — «с оговорками»; 0–2 met — «негодно»."""


def build_prompt(document: str) -> str:
    return PROMPT_TEMPLATE.format(rubric=README_RUBRIC, document=document)


def validate_response(raw: str) -> dict | None:
    """Строгий JSON: verdict из домена, criteria — список объектов {key, met: bool}."""
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
    if data.get("verdict") not in VERDICTS:
        return None
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        return None
    for item in criteria:
        if not isinstance(item, dict) or not item.get("key") or "met" not in item:
            return None
        if not isinstance(item["met"], bool):
            return None
    # находка 4 ревью: ровно 5 критериев, ключи — домен рубрики, без дублей
    keys = [item["key"] for item in criteria]
    if sorted(keys) != sorted(CRITERIA_KEYS):
        return None
    return data


def rule_verdict(criteria: list[dict]) -> str:
    """Находка 1 ревью: вердикт считается КОДОМ по механическому правилу —
    слово модели используется только для сверки (расхождение = пометка в отчёте)."""
    met = sum(1 for c in criteria if c.get("met"))
    if met == 5:
        return "годно"
    if met >= 3:
        return "с оговорками"
    return "негодно"


def render_report(rows: list[dict], llm_model: str) -> str:
    from datetime import date

    lines = [
        f"# FR-11-пилот: качество README — {date.today().isoformat()} (#45)",
        "",
        f"> Рубрика README v{RUBRIC_VERSION} (утверждена решением CEO 2026-07-31).",
        f"> `llm_model`: **{llm_model}** (канон Б1). Скрипт: `evals/fr11_pilot.py`.",
        "> Назначение: материал занятия 15 + данные точности для решения о FR-11 в v2.",
        "> Отчёт локальный, в git не коммитится (цитаты из репозиториев студентов).",
        "",
        "| Репо | Вердикт | purpose | run | structure | status | honesty |",
        "|---|---|---|---|---|---|---|",
    ]
    order = CRITERIA_KEYS
    none_labels = {  # находки 2–3 ревью: сбой инфраструктуры — не оценка студента
        "no_readme": "README не найден",
        "llm_unavailable": "нет вердикта: LLM недоступна",
        "parse_error": "нет вердикта: ответ не распарсился",
    }
    for row in rows:
        if row["verdict"] is None:
            label = none_labels.get(row.get("reason"), "README не найден")
            lines.append(f"| {row['repo_label']} | {label} | — | — | — | — | — |")
            continue
        by_key = {c["key"]: c for c in row["criteria"]}
        marks = " | ".join(
            ("✅" if by_key[k]["met"] else "❌") if k in by_key else "?" for k in order
        )
        verdict = row["verdict"]
        model_verdict = row.get("model_verdict")
        if model_verdict and model_verdict != verdict:
            verdict = f"{verdict} (модель: {model_verdict} — расхождение)"
        lines.append(f"| {row['repo_label']} | {verdict} | {marks} |")
    lines.append("")
    for row in rows:
        if row["verdict"] is None:
            continue
        lines.append(f"## {row['repo_label']}")
        for c in row["criteria"]:
            mark = "✅" if c.get("met") else "❌"
            lines.append(f"- {mark} {c.get('key')}: {c.get('note', '')}")
        if row.get("notes"):
            lines.append(f"- Итог: {row['notes']}")
        lines.append("")
    lines += [
        "## Для решения о FR-11 (v2)",
        "- Ручная сверка преподавателя по каждой строке: согласен / не согласен — это и есть замер точности.",
        "- Порог доверия — тот же принцип, что у ядра (≤20% ложных, PRD §4).",
        "",
    ]
    return "\n".join(lines)


async def run_pilot() -> None:
    import httpx
    from sqlalchemy import select

    from app import store
    from app.clients.git_client import GitClient, GitClientError
    from app.config import settings
    from app.models import ArtifactRole
    from app.store import SessionLocal

    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY не задан в .env")

    # README по последним снапшотам роли readme из боевой БД
    targets = []
    with SessionLocal() as session:
        from app.models.repository import Repository

        repos = [r for r in session.scalars(select(Repository)) if r.archived_at is None]
        readme_defs = store.find_artifact_defs_by_role(session, ArtifactRole.readme)
        for idx, repo in enumerate(repos, start=1):
            snap = None
            for adef in readme_defs:
                candidate = store.find_last_snapshot(session, repo.id, adef.id)
                if candidate is not None and candidate.content_hash:
                    snap = candidate
                    break
            targets.append({
                "label": f"репо-{idx:02d} ({repo.id[:8]})",
                "repo_url": repo.repo_url,
                "git_host": repo.git_host,
                "file_path": snap.file_path if snap else None,
                "ref": (snap.source_commit_sha if snap else None) or repo.default_branch,
            })

    # скрипт вне продукта: свой httpx-клиент (LLMClient заточен под контракт связности §5.2)
    git = GitClient()
    rows = []
    try:
        async with httpx.AsyncClient(timeout=180.0) as http:
            for target in targets:
                if target["file_path"] is None:
                    rows.append({"repo_label": target["label"], "verdict": None,
                                 "reason": "no_readme", "criteria": [], "notes": None})
                    continue
                try:
                    content = await git.get_file_content(
                        target["repo_url"], target["git_host"], target["file_path"],
                        ref=target["ref"],
                    )
                except GitClientError as exc:
                    print(f"{target['label']}: репозиторий недоступен ({exc})", flush=True)
                    rows.append({"repo_label": target["label"], "verdict": None,
                                 "reason": "no_readme", "criteria": [], "notes": None})
                    continue
                print(f"оценка {target['label']}…", flush=True)
                validated, failed = None, False
                for _ in range(2):  # 1 ретрай — канон §5.2/мини-эвала (находка 2)
                    try:
                        response = await http.post(
                            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                            json={
                                "model": settings.deepseek_model,
                                "messages": [{"role": "user", "content": build_prompt(content)}],
                                "response_format": {"type": "json_object"},
                                "temperature": 0.0,
                            },
                        )
                        response.raise_for_status()
                        raw = response.json()["choices"][0]["message"]["content"]
                    except (httpx.HTTPError, KeyError, ValueError) as exc:
                        print(f"  {target['label']}: LLM недоступна ({exc})", flush=True)
                        rows.append({"repo_label": target["label"], "verdict": None,
                                     "reason": "llm_unavailable", "criteria": [], "notes": None})
                        failed = True
                        break
                    validated = validate_response(raw)
                    if validated is not None:
                        break
                if failed:
                    continue
                if validated is None:
                    # находка 2: сбой парсинга — не оценка студента
                    rows.append({"repo_label": target["label"], "verdict": None,
                                 "reason": "parse_error", "criteria": [], "notes": None})
                    continue
                computed = rule_verdict(validated["criteria"])  # находка 1: правило в коде
                rows.append({
                    "repo_label": target["label"],
                    "verdict": computed,
                    "model_verdict": validated["verdict"],
                    "criteria": validated["criteria"],
                    "notes": validated.get("notes"),
                })
    finally:
        await git.aclose()

    report = render_report(rows, llm_model=settings.deepseek_model)
    from datetime import date

    out = ROOT / "evals" / f"отчёт-fr11-пилот-{date.today().isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(f"отчёт: {out}")
    for row in rows:
        print(f"  {row['repo_label']}: {row['verdict'] or 'README не найден'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="FR-11-пилот: качество README (#45)")
    parser.parse_args()
    asyncio.run(run_pilot())


if __name__ == "__main__":
    main()
