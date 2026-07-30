# H1 (#52): тесты git-хуков — машинный гейт стандарта тестирования п.3
# («существующие тесты — контракт; изменение теста = изменение требования»).
# Хук hooks/commit-msg блокирует commit, меняющий/удаляющий существующие файлы tests/,
# если в сообщении нет строки «tests-change: <обоснование>». Новые тесты проходят свободно.
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PROJECT_ROOT / "hooks"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Временный git-репозиторий с хуками проекта и одним закоммиченным тестом."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "hooks-test@example.com")
    git(tmp_path, "config", "user.name", "Hooks Test")
    shutil.copytree(HOOKS_DIR, tmp_path / "hooks")
    git(tmp_path, "config", "core.hooksPath", "hooks")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert git(tmp_path, "add", "-A").returncode == 0
    assert git(tmp_path, "commit", "-q", "-m", "init").returncode == 0
    return tmp_path


def test_modify_existing_test_without_marker_is_blocked(repo: Path):
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 == 1\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    result = git(repo, "commit", "-m", "правлю тест ради зелёного")
    assert result.returncode != 0
    assert "tests-change" in (result.stdout + result.stderr)


def test_delete_existing_test_without_marker_is_blocked(repo: Path):
    (repo / "tests" / "test_sample.py").unlink()
    git(repo, "add", "-A")
    result = git(repo, "commit", "-m", "удаляю неудобный тест")
    assert result.returncode != 0


def test_modify_existing_test_with_marker_passes(repo: Path):
    (repo / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 == 1\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    result = git(
        repo,
        "commit",
        "-m",
        "фикс контракта\n\ntests-change: требование изменено тикетом #52",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_test_file_passes_without_marker(repo: Path):
    (repo / "tests" / "test_new_feature.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    result = git(repo, "commit", "-m", "новый тест — свободно")
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_test_change_passes_without_marker(repo: Path):
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "-A")
    result = git(repo, "commit", "-m", "обычная правка кода")
    assert result.returncode == 0, result.stdout + result.stderr


def test_coverage_gate_configured():
    # Гейт «политика в тексте ≠ машинный гейт»: fail_under зафиксирован в pyproject.
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "fail_under" in text
