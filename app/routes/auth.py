"""S5 (#7), FR-0: login/logout с bcrypt и блокировкой 15 минут после 5 неудач.

S76 (#76): события входа логируются — успех, неудача (с номером попытки),
срабатывание и действие блокировки. До этого вопрос «кто входил с незнакомого
IP» по журналу не решался вовсе (аудит боевого журнала 2026-08-14).
Контракт #75 (NFR-3): сырые идентификаторы в журнал не пишутся — метка
оператора это user_marker; неизвестное имя не пишется тем более (в поле имени
по ошибке вводят пароль).
"""

import logging
from datetime import timedelta
from urllib.parse import parse_qs

import bcrypt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import store
from app.logging_config import user_marker
from app.routes import get_session, templates
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"

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
    now = utcnow()

    ip = _client_ip(request)

    if user and user.locked_until and user.locked_until > now:
        logger.warning(
            "Вход при активной блокировке: пользователь=%s ip=%s", user_marker(user.id), ip
        )
        return templates.TemplateResponse(
            request, "login.html", {"error": "Учётная запись заблокирована на 15 минут"}, status_code=429
        )

    if user and _password_matches(password, user.password_hash):
        store.update_user_lockout(session, user.id, failed_attempts=0, locked_until=None)
        request.session["user_id"] = user.id
        logger.info("Вход выполнен: пользователь=%s ip=%s", user_marker(user.id), ip)
        return RedirectResponse("/", status_code=303)

    if user:
        failed = user.failed_attempts + 1
        locked_until = now + timedelta(minutes=LOCKOUT_MINUTES) if failed >= MAX_FAILED_ATTEMPTS else None
        store.update_user_lockout(session, user.id, failed_attempts=failed, locked_until=locked_until)
        logger.warning(
            "Неудачный вход: пользователь=%s попытка=%d ip=%s", user_marker(user.id), failed, ip
        )
        if locked_until is not None:
            logger.warning(
                "Учётная запись заблокирована на %d минут после %d неудач: пользователь=%s ip=%s",
                LOCKOUT_MINUTES, failed, user_marker(user.id), ip,
            )
    else:
        # сырое значение поля имени не логируется: туда по ошибке вводят пароль
        logger.warning("Неудачный вход: имя не найдено ip=%s", ip)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
