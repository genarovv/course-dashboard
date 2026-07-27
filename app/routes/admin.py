"""Админ-роуты (§3.1). S6 (#8): POST /import-csv. S4 (#6): POST /admin/reload-config. POST /sync — G2 (#9)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.clients.git_client import GitClient
from app.routes import get_session
from app.services import config_manager, csv_importer

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


@router.post("/admin/reload-config")
async def reload_config(request: Request, session: Session = Depends(get_session)):
    """FR-2 (S4, #6): перечитать config.yaml и реконсилировать БД (ADR-005)."""
    if "user_id" not in request.session:  # BR-4: teacher-only
        return JSONResponse({"error": "не аутентифицирован"}, status_code=401)
    summary = config_manager.reconcile(session, config_manager.load_config())
    return JSONResponse(summary.model_dump())
