"""S3 (#1): миграция + DDL-инварианты — критерии приёмки как регрессионные тесты."""

import sqlite3
import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command
from app.models import Base

TABLES = [
    "system_user", "lesson", "rubric", "artifact_def", "edge_def", "repository",
    "git_credential", "sync_run", "sync_run_repository", "artifact_snapshot",
    "coherence_verdict", "override",
]


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def db(tmp_path):
    """БД, созданная alembic upgrade head (как в проде)."""
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()
    command.downgrade(cfg, "base")  # критерий приёмки: откат без ошибок


def _seed_minimal(db):
    """Минимальный граф: rubric, lesson, artifact_def, edge_def, repository, sync_run."""
    ids = {k: _uid() for k in ("rubric", "lesson", "adef", "edge", "repo", "run")}
    db.execute(
        "INSERT INTO rubric (id, type, version, text, created_at) VALUES (?, 'edge', '1.0', 't', '2026-01-01')",
        (ids["rubric"],),
    )
    db.execute(
        "INSERT INTO lesson (id, number, title, date) VALUES (?, 1, 'l', '2026-01-01')", (ids["lesson"],)
    )
    db.execute(
        "INSERT INTO artifact_def (id, lesson_id, role, expected_pattern) VALUES (?, ?, 'prd', '**/prd.md')",
        (ids["adef"], ids["lesson"]),
    )
    db.execute(
        "INSERT INTO edge_def (id, source_role, target_role, rubric_id) VALUES (?, 'prd', 'data_model', ?)",
        (ids["edge"], ids["rubric"]),
    )
    db.execute(
        "INSERT INTO repository (id, repo_url, normalized_repo_url, git_host) VALUES (?, 'u', 'u', 'GitHub')",
        (ids["repo"],),
    )
    db.execute("INSERT INTO sync_run (id, triggered_by) VALUES (?, 'manual')", (ids["run"],))
    return ids


def _new_run(db) -> str:
    run_id = _uid()
    db.execute("INSERT INTO sync_run (id, triggered_by) VALUES (?, 'manual')", (run_id,))
    return run_id


def _insert_snapshot(db, ids, status="found", content_hash="h1", partial_reason=None, run_id=None):
    sid = _uid()
    db.execute(
        "INSERT INTO artifact_snapshot (id, sync_run_id, repository_id, artifact_def_id,"
        " status, partial_reason, content_hash, observed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01')",
        (sid, run_id or ids["run"], ids["repo"], ids["adef"], status, partial_reason, content_hash),
    )
    return sid


def _insert_verdict(db, ids, sid, verdict="ok", hashes=("h1", "h2")):
    vid = _uid()
    db.execute(
        "INSERT INTO coherence_verdict (id, edge_def_id, source_snapshot_id, target_snapshot_id,"
        " source_content_hash, target_content_hash, rubric_id, llm_model, computed_at, verdict, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'm', '2026-01-01', ?, 'high')",
        (vid, ids["edge"], sid, sid, hashes[0], hashes[1], ids["rubric"], verdict),
    )
    return vid


def test_upgrade_creates_11_tables_and_seed(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= tables
    # И10 single-user: сид одной строки system_user при миграции
    assert db.execute("SELECT COUNT(*) FROM system_user").fetchone()[0] == 1


def test_metadata_matches_ddl(db):
    """ORM-метаданные компилируются (sqlite_where, CHECK) — create_all не падает."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    engine.dispose()


def test_i1_override_xor(db):
    ids = _seed_minimal(db)
    sid = _insert_snapshot(db, ids)
    vid = _insert_verdict(db, ids, sid)
    with pytest.raises(sqlite3.IntegrityError):  # обе ссылки NULL
        db.execute("INSERT INTO override (id, reason, created_at) VALUES (?, 'r', '2026-01-01')", (_uid(),))
    db.execute(
        "INSERT INTO override (id, coherence_verdict_id, reason, created_at) VALUES (?, ?, 'r', '2026-01-01')",
        (_uid(), vid),
    )


def test_i3_quad_unique_except_deferred(db):
    ids = _seed_minimal(db)
    sid = _insert_snapshot(db, ids)
    _insert_verdict(db, ids, sid, verdict="ok")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_verdict(db, ids, sid, verdict="break")  # та же четвёрка
    _insert_verdict(db, ids, sid, verdict="deferred", hashes=("h1", "h2"))  # deferred исключён
    _insert_verdict(db, ids, sid, verdict="deferred", hashes=("h1", "h2"))


def test_i4_one_active_override(db):
    ids = _seed_minimal(db)
    sid = _insert_snapshot(db, ids)
    vid = _insert_verdict(db, ids, sid)
    db.execute(
        "INSERT INTO override (id, coherence_verdict_id, reason, created_at) VALUES (?, ?, 'r', '2026-01-01')",
        (_uid(), vid),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO override (id, coherence_verdict_id, reason, created_at)"
            " VALUES (?, ?, 'r2', '2026-01-01')",
            (_uid(), vid),
        )


def test_i5_append_only_triggers(db):
    ids = _seed_minimal(db)
    sid = _insert_snapshot(db, ids)
    vid = _insert_verdict(db, ids, sid)
    for stmt in (
        ("UPDATE artifact_snapshot SET status='partial' WHERE id=?", sid),
        ("DELETE FROM artifact_snapshot WHERE id=?", sid),
        ("UPDATE coherence_verdict SET verdict='break' WHERE id=?", vid),
        ("DELETE FROM coherence_verdict WHERE id=?", vid),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(*[stmt[0], (stmt[1],)])


def test_i6_normalized_url_unique(db):
    _seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO repository (id, repo_url, normalized_repo_url, git_host)"
            " VALUES (?, 'u.git', 'u', 'GitHub')",
            (_uid(),),
        )


def test_i8_snapshot_consistency(db):
    ids = _seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):  # partial без причин
        _insert_snapshot(db, ids, status="partial", partial_reason=None)
    with pytest.raises(sqlite3.IntegrityError):  # found без хеша
        _insert_snapshot(db, ids, status="found", content_hash=None)
    with pytest.raises(sqlite3.IntegrityError):  # not_found с хешем
        _insert_snapshot(db, ids, status="not_found", content_hash="h1")
    _insert_snapshot(db, ids, status="partial", partial_reason='["template_copy"]')
    _insert_snapshot(db, ids, status="not_found", content_hash=None, run_id=_new_run(db))  # И9: свежий обход


def test_i9_i11_uniqueness(db):
    ids = _seed_minimal(db)
    _insert_snapshot(db, ids)
    with pytest.raises(sqlite3.IntegrityError):  # И9: второй снапшот на тройку
        _insert_snapshot(db, ids, content_hash="h2")
    db.execute(
        "INSERT INTO sync_run_repository (sync_run_id, repository_id, outcome, checked_at)"
        " VALUES (?, ?, 'ok_changed', '2026-01-01')",
        (ids["run"], ids["repo"]),
    )
    with pytest.raises(sqlite3.IntegrityError):  # И11: второй охват на пару
        db.execute(
            "INSERT INTO sync_run_repository (sync_run_id, repository_id, outcome, checked_at)"
            " VALUES (?, ?, 'ok_unchanged', '2026-01-01')",
            (ids["run"], ids["repo"]),
        )


def test_i10_reference_uniqueness(db):
    ids = _seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):  # lesson.number
        db.execute("INSERT INTO lesson (id, number, title, date) VALUES (?, 1, 'x', '2026-01-02')", (_uid(),))
    with pytest.raises(sqlite3.IntegrityError):  # edge_def (source, target)
        db.execute(
            "INSERT INTO edge_def (id, source_role, target_role, rubric_id)"
            " VALUES (?, 'prd', 'data_model', ?)",
            (_uid(), ids["rubric"]),
        )
    db.execute("INSERT INTO git_credential (id, git_host, checked_at) VALUES (?, 'GitHub', '2026-01-01')", (_uid(),))
    with pytest.raises(sqlite3.IntegrityError):  # git_credential.git_host
        db.execute(
            "INSERT INTO git_credential (id, git_host, checked_at) VALUES (?, 'GitHub', '2026-01-01')", (_uid(),)
        )
