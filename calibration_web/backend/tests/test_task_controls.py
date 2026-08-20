from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ego_web.main import create_app
from ego_web.runner import RunnerResult
from ego_web.settings import Settings


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.paused: set[str] = set()

    def run(self, task: dict[str, Any], on_stage: Callable[[str], None]) -> RunnerResult:
        on_stage("camera_calibration")
        self.started.set()
        self.release.wait(3)
        return RunnerResult(exit_code=0, success=True)

    def pause(self, task_id: str) -> bool:
        if not self.started.is_set() or task_id in self.paused:
            return False
        self.paused.add(task_id)
        return True

    def resume(self, task_id: str) -> bool:
        if task_id not in self.paused:
            return False
        self.paused.remove(task_id)
        return True

    def terminate_all(self) -> None:
        self.release.set()


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


def wait_for_status(client: TestClient, task_id: str, status: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] == status:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task did not reach {status}")


def test_running_task_can_pause_and_resume(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-a")
    runner = BlockingRunner()
    app = create_app(settings=settings, runner=runner)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/task-groups", json={"datasets": ["dataset-a"]}
        ).json()
        task_id = created["task_ids"][0]
        assert runner.started.wait(2)
        wait_for_status(client, task_id, "running")

        paused = client.post(f"/api/v1/tasks/{task_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["stage"] == "paused"
        assert task_id in runner.paused

        resumed = client.post(f"/api/v1/tasks/{task_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["stage"] == "camera_calibration"
        assert task_id not in runner.paused
        runner.release.set()


def test_queued_task_can_be_deleted(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-a")
    runner = BlockingRunner()
    app = create_app(settings=settings, runner=runner)

    with TestClient(app) as client:
        created = app.state.task_service.create_task_group(datasets=["dataset-a"])
        task_id = created.task_ids[0]
        task_root = settings.runtime_root / "tasks" / task_id
        response = client.delete(f"/api/v1/tasks/{task_id}")

        assert response.status_code == 200
        assert response.json() == {"task_id": task_id, "deleted": True}
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
        assert not task_root.exists()


def test_terminal_task_and_runtime_log_can_be_deleted_without_removing_results(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    dataset = make_dataset(settings.data_root, "dataset-a")
    result = dataset / "result"
    result.mkdir()
    (result / "summary.txt").write_text("OK\n")
    app = create_app(settings=settings, runner=BlockingRunner())

    with TestClient(app) as client:
        created = app.state.task_service.create_task_group(datasets=["dataset-a"])
        task_id = created.task_ids[0]
        task_root = settings.runtime_root / "tasks" / task_id
        (task_root / "runner.log").write_text("finished\n")
        app.state.database.transition_task(
            task_id,
            "failed",
            finished_at="2026-08-20T10:10:00Z",
            exit_code=1,
        )

        response = client.delete(f"/api/v1/tasks/{task_id}")

        assert response.status_code == 200
        assert response.json() == {"task_id": task_id, "deleted": True}
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
        assert not task_root.exists()
        assert (result / "summary.txt").read_text() == "OK\n"
        assert all((dataset / name).is_file() for name in (
            "calibration_4cam.bag",
            "calibration_cam0_imu.bag",
            "imu.bag",
        ))


def test_running_task_cannot_be_deleted(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-a")
    runner = BlockingRunner()
    app = create_app(settings=settings, runner=runner)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/task-groups", json={"datasets": ["dataset-a"]}
        ).json()
        task_id = created["task_ids"][0]
        assert runner.started.wait(2)
        wait_for_status(client, task_id, "running")
        response = client.delete(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 409
        runner.release.set()
