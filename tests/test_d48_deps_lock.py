"""D48 (#79): зависимости под контролем — лок версий и диета uvicorn.

Повод — разбор зависимостей с CEO 2026-08-14 после инцидента с затёртым venv:
из 40 пакетов 13 выбраны осознанно, 24 — обязательный транзитив стека, а
3 (websockets, httptools, watchfiles) — экстры `uvicorn[standard]`, которых
код проекта не упоминает ни разу. Плюс проект задавал только нижние границы
версий — каждая свежая установка тянула новейшие версии (в день восстановления
venv это дало разовый флак теста на новых версиях).

Контракт:
  * `requirements.lock` — полный список пакетов с точными версиями (`==`),
    единственный источник для установки (CLAUDE.md: зависимости ставит CEO
    по lock); обновляется только вместе с согласованным изменением pyproject;
  * каждая прямая зависимость pyproject (включая dev) закреплена в lock;
  * uvicorn без extras: вебсокетов в приложении нет, httptools — не нужное
    ускорение, watchfiles — только для --reload (uvicorn без него деградирует
    до встроенного StatReload, dev-цикл не ломается).
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"
PYPROJECT = ROOT / "pyproject.toml"

UVICORN_EXTRAS = {"websockets", "httptools", "watchfiles"}


def _lock_names() -> dict[str, str]:
    """{имя-в-нижнем-регистре: версия} из requirements.lock."""
    names = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"строка lock без точной версии: {line!r}"
        name, version = line.split("==", 1)
        names[name.lower().replace("_", "-")] = version
    return names


def _direct_deps() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = list(data["project"]["dependencies"])
    for group in data["project"].get("optional-dependencies", {}).values():
        deps.extend(group)
    return deps


def _dep_name(spec: str) -> str:
    return re.split(r"[\[<>=!~;\s]", spec, 1)[0].lower().replace("_", "-")


def test_lock_exists_and_every_line_is_pinned():
    assert LOCK.exists(), "requirements.lock отсутствует — установка не воспроизводима"
    assert len(_lock_names()) >= 30  # полный транзитив, а не только прямые


def test_every_direct_dependency_is_pinned_in_lock():
    lock = _lock_names()
    missing = [d for d in map(_dep_name, _direct_deps()) if d not in lock]
    assert not missing, f"прямые зависимости без пина в lock: {missing}"


def test_uvicorn_has_no_standard_extra():
    """Диета: `uvicorn[standard]` тянул websockets/httptools/watchfiles —
    ни один не упомянут в app/ (проверено grep 2026-08-14)."""
    uvicorn_specs = [d for d in _direct_deps() if _dep_name(d) == "uvicorn"]
    assert uvicorn_specs, "uvicorn пропал из зависимостей"
    for spec in uvicorn_specs:
        assert "[" not in spec, f"у uvicorn остались extras: {spec!r}"


def test_standard_extras_are_not_in_lock():
    """Lock — источник установки: пока экстры в нём, диета не действует."""
    leftovers = UVICORN_EXTRAS & set(_lock_names())
    assert not leftovers, f"экстры uvicorn[standard] остались в lock: {sorted(leftovers)}"
