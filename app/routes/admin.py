"""Админ-роуты (§3.1). S6 (#8): POST /import-csv. S4 (#6): POST /admin/reload-config. G2 (#9): POST /sync."""

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.clients.git_client import GitClient
from app.config import settings
from app.models import SyncTrigger
from app.routes import get_session
from app.services import config_manager, csv_importer, sync_orchestrator

router = APIRouter()


def get_git_client() -> GitClient:
    return GitClient()


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
):
    """FR-8 (G2, #9): полный обход. Доступ: сессия преподавателя или X-Sync-Token (cron, §5.5)."""
    token = request.headers.get("X-Sync-Token", "")
    token_ok = bool(settings.sync_token) and secrets.compare_digest(token, settings.sync_token)
    if "user_id" not in request.session and not token_ok:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    triggered_by = SyncTrigger.schedule if token_ok else SyncTrigger.manual
    run = await sync_orchestrator.run_sync(session, git_client, triggered_by=triggered_by)
    return JSONResponse({"sync_run_id": run.id, "status": run.status})


@router.post("/admin/reload-config")
async def reload_config(request: Request, session: Session = Depends(get_session)):
    """FR-2 (S4, #6): перечитать config.yaml и реконсилировать БД (ADR-005)."""
    if "user_id" not in request.session:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    summary = config_manager.reconcile(session, config_manager.load_config())
    return JSONResponse(summary.model_dump())
