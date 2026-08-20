from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ego_web.main import create_app
from ego_web.runner import RunnerResult
from ego_web.settings import Settings


class ImmediateRunner:
    def __init__(self) -> None:
        self.datasets: list[str] = []
        self.terminate_called = False

    def run(self, task: dict[str, Any], on_stage: Callable[[str], None]) -> RunnerResult:
        self.datasets.append(str(task["dataset"]))
        on_stage("imu_calibration")
        on_stage("publishing")
        return RunnerResult(exit_code=0, success=True)

    def terminate_all(self) -> None:
        self.terminate_called = True


def make_settings(tmp_path: Path) -> Settings:
    repo_root = tmp_path / "repo"
    runner = repo_root / "auto_calib" / "run_all.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/bash\n")
    frontend = repo_root / "calibration_web" / "frontend" / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<div>KALIBR WEB</div>")
    return Settings(
        data_root=tmp_path / "data",
        repo_root=repo_root,
        runtime_root=tmp_path / "runtime",
        max_concurrency=1,
    )


def test_dataset_catalog_health_and_spa(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    ready = make_dataset(settings.data_root, "ready-set")
    completed = make_dataset(settings.data_root, "completed-set")
    (completed / "result").mkdir()
    (completed / "result" / ".done").write_text("done")
    app = create_app(settings=settings, runner=ImmediateRunner())

    with TestClient(app) as client:
        catalog = client.get("/api/v1/datasets")
        health = client.get("/api/v1/health")
        spa = client.get("/tasks")
        missing_api = client.get("/api/v1/no-such-route")

    assert ready.is_dir()
    assert catalog.status_code == 200
    assert catalog.json() == {"items": [
        {"name": "completed-set", "state": "completed", "active_task_id": None},
        {"name": "ready-set", "state": "ready", "active_task_id": None},
    ]}
    assert health.json()["status"] == "ok"
    assert spa.status_code == 200 and "KALIBR WEB" in spa.text
    assert missing_api.status_code == 404


def test_submit_lists_filters_and_hides_task_root(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "set-a")
    make_dataset(settings.data_root, "set-b")
    runner = ImmediateRunner()
    app = create_app(settings=settings, runner=runner)

    with TestClient(app) as client:
        response = client.post("/api/v1/task-groups", json={"datasets": ["set-a", "set-b"]})
        assert response.status_code == 201
        task_ids = response.json()["task_ids"]
        deadline = time.monotonic() + 2
        while len(runner.datasets) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        tasks = client.get("/api/v1/tasks", params={"dataset": "set-a"}).json()["items"]
        detail = client.get(f"/api/v1/tasks/{task_ids[0]}").json()
        group = client.get(f"/api/v1/task-groups/{response.json()['group_id']}").json()

    assert sorted(runner.datasets) == ["set-a", "set-b"]
    assert len(tasks) == 1 and tasks[0]["dataset"] == "set-a"
    assert "task_root" not in detail
    assert all("task_root" not in task for task in group["tasks"])


def test_submission_validation_and_runtime(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "set-a")
    app = create_app(settings=settings, runner=ImmediateRunner())

    with TestClient(app) as client:
        duplicate = client.post("/api/v1/task-groups", json={"datasets": ["set-a", "set-a"]})
        missing = client.post("/api/v1/task-groups", json={"datasets": ["missing"]})
        extra = client.post("/api/v1/task-groups", json={"datasets": ["set-a"], "args": "unsafe"})
        runtime = client.get("/api/v1/runtime")

    assert duplicate.status_code == 422
    assert missing.status_code == 422
    assert extra.status_code == 422
    assert runtime.json() == {
        "max_concurrency": 1,
        "running": 0,
        "queued": 0,
        "accepting_tasks": True,
    }


def test_lifespan_stops_runner(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.data_root.mkdir()
    runner = ImmediateRunner()
    app = create_app(settings=settings, runner=runner)
    with TestClient(app) as client:
        assert client.get("/api/v1/runtime").status_code == 200
    assert runner.terminate_called is True
