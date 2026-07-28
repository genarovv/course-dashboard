"""Дашборд: GET / — матрица (D1, #12; FR-4); GET /students/{id} — карточка (D4, #14; FR-9)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import store
from app.routes import get_session, templates
from app.services.evidence_chain import build_student_card
from app.services.matrix_builder import build_matrix

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: Session = Depends(get_session)):
    if "user_id" not in request.session:
        return RedirectResponse("/login", status_code=303)
    matrix = build_matrix(session)
    return templates.TemplateResponse(request, "dashboard/matrix.html", {"matrix": matrix})


@router.post("/verdicts/{verdict_id}/override-toggle")
async def override_toggle(
    verdict_id: str, request: Request, session: Session = Depends(get_session)
):
    """FR-10 (O2, #16): отметка «ложный разрыв» — создать / снять (revoked_at, не удаление)."""
    if "user_id" not in request.session:  # BR-4: teacher-only
        return RedirectResponse("/login", status_code=303)
    if store.find_verdict_by_id(session, verdict_id) is None:
        raise HTTPException(status_code=404, detail="вердикт не найден")
    active = store.find_active_override_for_verdict(session, verdict_id)
    if active is not None:
        store.update_override_revoked(session, active.id)
    else:
        store.register_override(
            session, coherence_verdict_id=verdict_id, reason="отмечено преподавателем в UI"
        )
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@router.get("/students/{repository_id}")
async def student_card(
    repository_id: str, request: Request, session: Session = Depends(get_session)
):
    if "user_id" not in request.session:  # BR-4: teacher-only
        return RedirectResponse("/login", status_code=303)
    card = build_student_card(session, repository_id)
    if card is None:
        raise HTTPException(status_code=404, detail="репозиторий не найден")
    return templates.TemplateResponse(request, "dashboard/student_card.html", {"card": card})
