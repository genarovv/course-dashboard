# Гейт окружения pre-push: git экспортирует GIT_DIR при запуске хука из linked
# worktree, и утечка ломает изолированные git-репозитории в tests/test_hooks.py.
# Хук обязан очищать git-переменные перед запуском тестов.
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_pre_push_unsets_git_env_for_tests():
    text = (PROJECT_ROOT / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert "unset GIT_DIR" in text
