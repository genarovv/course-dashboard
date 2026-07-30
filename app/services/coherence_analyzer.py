"""C2 (#36), FR-5: ядро — вердикт связности пары артефактов по контракту §5.2.

Гейт Фазы 0 снят 2026-07-30 (мини-эвал: golden set 2/2, ADR-004 Accepted) —
железное правило CLAUDE.md выполнено, ядро разблокировано решением CEO.

ensure_verdict:
  find_verdict_by_quadruple (D25 — «не мигаем»; deferred не находит → пересчёт)
  → И2: оба снапшота принадлежат репозиторию пары, роли соответствуют ребру
  → тексты из git по file_path @ source_commit_sha (контент не храним — С4)
  → llm_client.check_coherence → register_verdict ok/break
  → None → deferred(parse_error); LLMUnavailableError → deferred(llm_unavailable);
    GitClientError → ничего не пишем: пара без вердикта вернётся в следующий свод (§5.1).
"""

import asyncio
import logging

from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClientError
from app.clients.llm_client import LLMUnavailableError
from app.models import DeferredReason, VerdictValue
from app.models.coherence_verdict import CoherenceVerdict
from app.services.sync_orchestrator import PendingPair

logger = logging.getLogger(__name__)


def _check_i2(session: Session, pair: PendingPair, snap_a, snap_b) -> None:
    """И2: оба снапшота одного репозитория, роли артефактов соответствуют ребру."""
    if snap_a is None or snap_b is None:
        raise ValueError(f"И2: снапшоты пары не найдены (edge={pair.edge_def_id})")
    if not (snap_a.repository_id == snap_b.repository_id == pair.repository_id):
        raise ValueError(
            f"И2: снапшоты из разных репозиториев (пара репо={pair.repository_id})"
        )
    edge = store.find_edge_def_by_id(session, pair.edge_def_id)
    role_a = store.find_artifact_def_by_id(session, snap_a.artifact_def_id).role
    role_b = store.find_artifact_def_by_id(session, snap_b.artifact_def_id).role
    if (role_a, role_b) != (edge.source_role, edge.target_role):
        raise ValueError(
            f"И2: роли снапшотов ({role_a}→{role_b}) не соответствуют ребру "
            f"({edge.source_role}→{edge.target_role})"
        )


def _register(session: Session, pair: PendingPair, **fields) -> CoherenceVerdict:
    return store.register_verdict(
        session,
        edge_def_id=pair.edge_def_id,
        source_snapshot_id=pair.source_snapshot_id,
        target_snapshot_id=pair.target_snapshot_id,
        source_content_hash=pair.source_content_hash,
        target_content_hash=pair.target_content_hash,
        rubric_id=pair.rubric_id,
        llm_model=pair.llm_model,
        **fields,
    )


async def ensure_verdict(
    session: Session, git_client, llm_client, pair: PendingPair
) -> CoherenceVerdict | None:
    """Вердикт для пары: существующий (D25), новый от LLM или deferred.

    None — репозиторий не прочитался (GitClientError): не вина модели и не повод
    для deferred; пара без вердикта автоматически попадёт в следующий свод.
    """
    existing = store.find_verdict_by_quadruple(
        session,
        source_content_hash=pair.source_content_hash,
        target_content_hash=pair.target_content_hash,
        rubric_id=pair.rubric_id,
        llm_model=pair.llm_model,
    )
    if existing is not None:
        return existing  # D25: валидный вердикт не мигает

    snap_a = store.find_snapshot_by_id(session, pair.source_snapshot_id)
    snap_b = store.find_snapshot_by_id(session, pair.target_snapshot_id)
    _check_i2(session, pair, snap_a, snap_b)

    repo = store.find_repository_by_id(session, pair.repository_id)
    rubric = store.find_rubric_by_id(session, pair.rubric_id)
    try:
        source_text = await git_client.get_file_content(
            repo.repo_url, repo.git_host, snap_a.file_path,
            ref=snap_a.source_commit_sha or repo.default_branch,
        )
        target_text = await git_client.get_file_content(
            repo.repo_url, repo.git_host, snap_b.file_path,
            ref=snap_b.source_commit_sha or repo.default_branch,
        )
    except GitClientError as exc:
        logger.warning("Пара %s→%s: репозиторий недоступен (%s) — вердикт отложен до "
                       "следующего свода", snap_a.file_path, snap_b.file_path, exc)
        return None

    try:
        validated = await llm_client.check_coherence(source_text, target_text, rubric.text)
    except LLMUnavailableError as exc:
        logger.warning("Пара %s→%s: LLM недоступна (%s)", snap_a.file_path, snap_b.file_path, exc)
        return _register(
            session, pair,
            verdict=VerdictValue.deferred,
            deferred_reason=DeferredReason.llm_unavailable,
            confidence="low",
        )
    if validated is None:
        return _register(
            session, pair,
            verdict=VerdictValue.deferred,
            deferred_reason=DeferredReason.parse_error,
            confidence="low",
        )
    return _register(
        session, pair,
        verdict=VerdictValue(validated["verdict"]),
        confidence=validated["confidence"],
        entities_checked=validated["entities_checked"],
        entities_found=validated["entities_found"],
        entities_excluded=validated["entities_excluded"],
        entities_lost=validated["entities_lost"],
        points=validated["points"] or None,
        notes=validated.get("notes") or None,
    )


def make_verdict_worker(session_factory, git_client, llm_client):
    """Воркер для свода G4 (инъекция в run_sync/reconcile_llm_pairs).

    Каждая пара — собственная сессия с commit; записи сериализованы Lock-ом
    (единый writer SQLite, обход и так асинхронен вне тайминга NFR-1).
    """
    lock = asyncio.Lock()

    async def worker(pair: PendingPair) -> None:
        async with lock:
            with session_factory() as session:
                try:
                    verdict = await ensure_verdict(session, git_client, llm_client, pair)
                    if verdict is not None:
                        session.commit()
                except Exception:
                    logger.exception("Воркер вердиктов: пара edge=%s repo=%s не обработана",
                                     pair.edge_def_id, pair.repository_id)

    return worker
