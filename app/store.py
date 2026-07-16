"""Единая точка доступа к данным (ARCHITECTURE §3.1, §3.5).

Здесь живут engine и фабрика сессий (файла database.py в структуре §3.1 нет).
Контракт S2 (register_* / find_* / 4 узких update_*) — тикет #3.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)
