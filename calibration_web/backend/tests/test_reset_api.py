from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ego_web.main import create_app
from ego_web.reset_api import ResetError, reset_dataset_result
from ego_web.runner import RunnerResult
from ego_web.settings import Settings


class IdleRunner:
    def run(self, task: dict[str, Any], on_stage: Callable[[str], None]) -> RunnerResult:
        return RunnerResult(exit_code=0, success=True)

    def terminate_all(self) -> None:
        pass


def make_settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repo"
    runner = repo / "auto_calib" / "run_all.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/bash\n")
    return Settings(
        data_root=tmp_path / "data",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
    )


def test_reset_removes_only_result_tree(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    data_root = tmp_path / "data"
    dataset = make_dataset(data_root, "dataset-a")
    result = dataset / "result"
    nested = result / "nested"
    nested.mkdir(parents=True)
    (result / ".done").write_text("done")
    (result / "imu.yaml").write_text("imu")
    (nested / "report.txt").write_text("report")

    assert reset_dataset_result(data_root, "dataset-a") is True
    assert not result.exists()
    assert (dataset / "imu.bag").is_file()
    assert reset_dataset_result(data_root, "dataset-a") is False


def test_reset_rejects_symlinks_without_touching_target(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    data_root = tmp_path / "data"
    dataset = make_dataset(data_root, "dataset-a")
    result = dataset / "result"
    result.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (result / "link").symlink_to(outside)

    try:
        reset_dataset_result(data_root, "dataset-a")
    except ResetError:
        pass
    else:
        raise AssertionError("unsafe result symlink should be rejected")
    assert outside.read_text() == "keep"
    assert result.is_dir()


def test_reset_api_refreshes_dataset_to_ready(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    dataset = make_dataset(settings.data_root, "dataset-a")
    (dataset / "result").mkdir()
    (dataset / "result" / ".done").write_text("done")
    app = create_app(settings=settings, runner=IdleRunner())

    with TestClient(app) as client:
        response = client.delete("/api/v1/datasets/dataset-a/result")
        catalog = client.get("/api/v1/datasets")

    assert response.status_code == 200
    assert response.json() == {"dataset": "dataset-a", "reset": True}
    assert catalog.json()["items"][0]["state"] == "ready"


def test_reset_api_rejects_active_task(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    dataset = make_dataset(settings.data_root, "dataset-a")
    (dataset / "result").mkdir()
    (dataset / "result" / ".done").write_text("done")
    app = create_app(settings=settings, runner=IdleRunner())

    with TestClient(app) as client:
        task_root = settings.runtime_root / "tasks" / "task-a"
        task_root.mkdir(parents=True)
        app.state.database.create_task_group(
            {"id": "group-a", "created_at": "2026-08-20T12:00:00Z"},
            [{
                "id": "task-a",
                "group_id": "group-a",
                "dataset": "dataset-a",
                "status": "queued",
                "created_at": "2026-08-20T12:00:00Z",
                "task_root": str(task_root),
            }],
        )
        response = client.delete("/api/v1/datasets/dataset-a/result")

    assert response.status_code == 409
    assert (dataset / "result" / ".done").is_file()
