"""Админ-роуты (§3.1). S6 (#8): POST /import-csv. S4 (#6): POST /admin/reload-config. G2 (#9): POST /sync."""

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import store
from app.clients.git_client import GitClient
from app.clients.llm_client import LLMClient
from app.config import settings
from app.logging_config import user_marker
from app.models import SyncTrigger
from app.routes import get_session
from app.services import config_manager, csv_importer, sync_orchestrator
from app.services.coherence_analyzer import make_verdict_worker
from app.store import SessionLocal

router = APIRouter()


def get_git_client() -> GitClient:
    return GitClient()


def get_llm_client():
    """C2 (#36): ядро FR-5 включается только при заданном ключе — иначе свод
    честно оставляет пары в состоянии «проверяется» (как до подключения).
    D6 (#37): возвращается ФАБРИКА — клиент создаётся на пару и закрывается
    воркером (fire-and-forget задачи живут дольше HTTP-запроса)."""
    if not settings.deepseek_api_key:
        return None
    return LLMClient


@router.post("/import-csv")
async def import_csv(
    request: Request,
    session: Session = Depends(get_session),
    git_client: GitClient = Depends(get_git_client),
):
    if "user_id" not in request.session:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    csv_text = (await request.body()).decode("utf-8-sig")  # тело запроса — CSV (без multipart)
    summary = await csv_importer.import_csv(session, csv_text, git_client)
    return JSONResponse(summary.model_dump())


@router.post("/sync")
async def sync(
    request: Request,
    session: Session = Depends(get_session),
    git_client: GitClient = Depends(get_git_client),
    llm_client_factory=Depends(get_llm_client),
):
    """FR-8 (G2, #9): полный обход. Доступ: сессия преподавателя или X-Sync-Token (cron, §5.5)."""
    token = request.headers.get("X-Sync-Token", "")
    token_ok = bool(settings.sync_token) and secrets.compare_digest(token, settings.sync_token)
    if "user_id" not in request.session and not token_ok:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    # D42: обход уже идёт — честный отказ вместо 500 `database is locked`
    # (транзакция обхода держит единственного писателя SQLite). Кнопка в UI
    # погашена, но вторая вкладка и cron до этой проверки доходят.
    if sync_orchestrator.is_sync_running(session):
        return JSONResponse({"error": "обход уже идёт — дождитесь завершения"}, status_code=409)
    triggered_by = SyncTrigger.schedule if token_ok else SyncTrigger.manual
    # #75: в лог идёт короткий хеш оператора, а не идентификатор (ПД — вне журнала);
    # cron — без оператора, его метка — сам триггер schedule.
    actor = user_marker(request.session.get("user_id")) if not token_ok else None
    # шаблон (PRD FR-4, D35) и маркеры недели (FR-12) — из config.yaml
    config = config_manager.load_config()
    # C2 (#36): ядро FR-5 подключено к своду — воркер в собственных сессиях (fire-and-forget)
    verdict_worker = (
        make_verdict_worker(SessionLocal, git_client, llm_client_factory)
        if llm_client_factory is not None else None
    )
    run = await sync_orchestrator.run_sync(
        session, git_client, triggered_by=triggered_by, actor=actor,
        template_repo=config.template_repo,
        process_markers=config.process_markers,
        verdict_worker=verdict_worker,
    )
    # #31: обход нуля репозиториев — не успех, а сигнал «реестр пуст»
    checked = len(store.find_active_repositories(session))
    warning = (
        "Реестр пуст — обходить нечего. Импортируйте репозитории: POST /import-csv."
        if checked == 0 else None
    )
    return JSONResponse({
        "sync_run_id": run.id,
        "status": run.status,
        "repositories_checked": checked,
        "warning": warning,
    })


@router.post("/admin/reload-config")
async def reload_config(request: Request, session: Session = Depends(get_session)):
    """FR-2 (S4, #6): перечитать config.yaml и реконсилировать БД (ADR-005)."""
    if "user_id" not in request.session:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    summary = config_manager.reconcile(session, config_manager.load_config())
    return JSONResponse(summary.model_dump())
