"""GET /health — жив ли процесс + счётчики из БД (I2, #13; ARCHITECTURE §5.4).

Открыт без аутентификации: отдаёт только агрегированные счётчики (без URL и ПД);
им пользуются мониторинг и cron-диагностика (§5.5).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import store
from app.config import settings
from app.routes import get_session
from app.services import sync_orchestrator

router = APIRouter()


@router.get("/health")
async def health(session: Session = Depends(get_session)):
    run = store.find_last_sync_run(session)
    last_sync = None
    if run is not None:
        last_sync = {
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "status": run.status,
        }
    counters = sync_orchestrator.build_health_counters(session, settings.deepseek_model)
    # #31: пустой реестр виден в диагностике и без обхода
    repositories = len(store.find_active_repositories(session))
    return {"status": "ok", "last_sync": last_sync, "repositories": repositories, **counters}
