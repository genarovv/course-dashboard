"""App factory (I1, #2). ARCHITECTURE §3.1: main.py — app factory, lifespan, middleware."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.routes import auth, dashboard, health


def create_app() -> FastAPI:
    application = FastAPI(title="Course Dashboard")
    application.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    application.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    application.include_router(auth.router)
    application.include_router(dashboard.router)
    application.include_router(health.router)
    return application


app = create_app()
