from pathlib import Path

import pytest
from pydantic import ValidationError

from ego_web.settings import Settings


_SETTING_ENV_VARS = (
    "EGO_WEB_DATA_ROOT",
    "EGO_WEB_REPO_ROOT",
    "EGO_WEB_RUNTIME_ROOT",
    "EGO_WEB_MAX_CONCURRENCY",
    "EGO_WEB_HOST",
    "EGO_WEB_PORT",
    "EGO_WEB_TERMINATE_GRACE_SEC",
)


def test_settings_defaults_match_kalibr_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.data_root == Path("/home/conanluo/kalibr_data")
    assert settings.repo_root == Path("/root/kalibr")
    assert settings.runtime_root == Path("/root/kalibr/calibration_web/runtime")
    assert settings.database_path == Path("/root/kalibr/calibration_web/runtime/kalibr.sqlite3")
    assert settings.runner_script == Path("/root/kalibr/auto_calib/run_all.sh")
    assert settings.frontend_dist == Path("/root/kalibr/calibration_web/frontend/dist")
    assert settings.max_concurrency == 1
    assert settings.host == "0.0.0.0"
    assert settings.port == 8020
    assert settings.terminate_grace_sec == 10.0

    for removed_name in (
        "config_root",
        "cam_workers",
        "runner_python",
        "extract_python",
        "okvis_binary",
        "extract_script",
    ):
        assert not hasattr(settings, removed_name)


def test_settings_accept_environment_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EGO_WEB_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EGO_WEB_REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("EGO_WEB_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("EGO_WEB_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("EGO_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("EGO_WEB_PORT", "9000")
    monkeypatch.setenv("EGO_WEB_TERMINATE_GRACE_SEC", "2.5")

    settings = Settings()

    assert settings.data_root == tmp_path / "data"
    assert settings.repo_root == tmp_path / "repo"
    assert settings.runtime_root == tmp_path / "runtime"
    assert settings.database_path == tmp_path / "runtime/kalibr.sqlite3"
    assert settings.runner_script == tmp_path / "repo/auto_calib/run_all.sh"
    assert settings.frontend_dist == tmp_path / "repo/calibration_web/frontend/dist"
    assert settings.max_concurrency == 1
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.terminate_grace_sec == 2.5


@pytest.mark.parametrize("value", ["0", "2"])
def test_settings_reject_concurrency_other_than_one(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EGO_WEB_MAX_CONCURRENCY", value)

    with pytest.raises(ValidationError):
        Settings()
