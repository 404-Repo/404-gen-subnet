from pathlib import Path

import pytest

from validation_service.settings import Settings


def test_loads_dotenv_and_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for variable in (
        "GITHUB_TOKEN",
        "GITHUB_REPO",
        "GITHUB_BRANCH",
        "MIN_CHECK_STATE_INTERVAL",
        "MAX_CHECK_STATE_INTERVAL",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / ".env").write_text(
        "GITHUB_TOKEN=dotenv-token\n"
        "GITHUB_REPO=test/from-dotenv\n"
        "GITHUB_BRANCH=dotenv-branch\n"
        "MIN_CHECK_STATE_INTERVAL=30\n"
        "MAX_CHECK_STATE_INTERVAL=300\n"
        "LOG_LEVEL=INFO\n"
        "UNRELATED_SERVICE_SETTING=ignored\n"
    )
    monkeypatch.setenv("GITHUB_BRANCH", "environment-branch")

    settings = Settings()

    assert settings.github_token.get_secret_value() == "dotenv-token"
    assert "dotenv-token" not in repr(settings)
    assert settings.github_repo == "test/from-dotenv"
    assert settings.github_branch == "environment-branch"
    assert settings.min_check_state_interval_seconds == 30
    assert settings.max_check_state_interval_seconds == 300
    assert settings.log_level == "INFO"
