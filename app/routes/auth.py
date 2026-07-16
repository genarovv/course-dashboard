"""FR-0: аутентификация. I1 (#2) — форма входа; логика login/logout — S5 (#7)."""

from fastapi import APIRouter, Request

from app.routes import templates

router = APIRouter()


@router.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html")
