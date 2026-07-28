"""GET / — дашборд: матрица «репозиторий × занятие» (D1, #12; FR-4)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.routes import get_session, templates
from app.services.matrix_builder import build_matrix

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: Session = Depends(get_session)):
    if "user_id" not in request.session:
        return RedirectResponse("/login", status_code=303)
    matrix = build_matrix(session)
    return templates.TemplateResponse(request, "dashboard/matrix.html", {"matrix": matrix})
