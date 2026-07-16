"""S5 (#7), FR-0: login/logout с bcrypt и блокировкой 15 минут после 5 неудач."""

from datetime import datetime, timedelta
from urllib.parse import parse_qs

import bcrypt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import store
from app.routes import get_session, templates

router = APIRouter()

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


def _password_matches(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # сентинел '!' из сида миграции — вход невозможен
        return False


@router.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
async def login(request: Request, session: Session = Depends(get_session)):
    # urlencoded-форма парсится stdlib: python-multipart не входит в зависимости
    form = parse_qs((await request.body()).decode())
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    user = store.find_user_by_username(session, username)
    now = datetime.utcnow()

    if user and user.locked_until and user.locked_until > now:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Учётная запись заблокирована на 15 минут"}, status_code=429
        )

    if user and _password_matches(password, user.password_hash):
        store.update_user_lockout(session, user.id, failed_attempts=0, locked_until=None)
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    if user:
        failed = user.failed_attempts + 1
        locked_until = now + timedelta(minutes=LOCKOUT_MINUTES) if failed >= MAX_FAILED_ATTEMPTS else None
        store.update_user_lockout(session, user.id, failed_attempts=failed, locked_until=locked_until)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
