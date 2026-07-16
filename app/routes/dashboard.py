"""GET / — дашборд (матрица — D1, #12). I1 (#2): редирект на /login без аутентификации."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.routes import templates

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    if "user_id" not in request.session:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "base.html")
