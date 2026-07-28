"""App factory (I1, #2). ARCHITECTURE §3.1: main.py — app factory, lifespan, middleware."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import store
from app.config import settings
from app.routes import admin, auth, dashboard, health
from app.services import config_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # FR-2 (S4, #6): при старте config_manager читает эталонный config.yaml (§3.4).
    # Файла нет — fail-fast: источник правды о конфигурации отсутствует.
    with store.SessionLocal() as session:
        config_manager.reconcile(session, config_manager.load_config())
        session.commit()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Course Dashboard", lifespan=lifespan)
    application.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    application.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    application.include_router(admin.router)
    application.include_router(auth.router)
    application.include_router(dashboard.router)
    application.include_router(health.router)
    return application


app = create_app()
