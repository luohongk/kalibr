from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import ego_web.task_service as task_service_module
from ego_web.db import Database
from ego_web.settings import Settings
from ego_web.task_service import TaskService


MakeDataset = Callable[[Path, str], Path]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime",
    )


def make_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def sequential_ids(*values: str) -> Callable[[], str]:
    identifiers: Iterator[str] = iter(values)
    return lambda: next(identifiers)


def task_record(
    task_id: str,
    dataset: str,
    *,
    group_id: str = "existing-group",
    status: str = "queued",
    task_root: str | None = None,
) -> dict[str, object]:
    return {
        "id": task_id,
        "group_id": group_id,
        "dataset": dataset,
        "status": status,
        "stage": None,
        "created_at": "2026-08-20T08:00:00Z",
        "task_root": task_root or f"/old-runtime/tasks/{task_id}",
    }


def add_task(
    database: Database,
    dataset: str,
    *,
    task_id: str = "existing-task",
    group_id: str = "existing-group",
    status: str = "queued",
) -> None:
    database.create_task_group(
        {"id": group_id, "created_at": "2026-08-20T08:00:00Z"},
        [task_record(task_id, dataset, group_id=group_id, status=status)],
    )


def test_create_group_preserves_request_order_and_creates_minimal_task_roots(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-b")
    make_dataset(settings.data_root, "dataset-a")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group-1", "task-b", "task-a"),
        now_factory=lambda: "2026-08-20T10:00:00Z",
    )

    created = service.create_task_group(datasets=["dataset-b", "dataset-a"])

    assert created.group_id == "group-1"
    assert created.task_ids == ["task-b", "task-a"]
    assert database.get_task_group("group-1") == {
        "id": "group-1",
        "created_at": "2026-08-20T10:00:00Z",
        "task_count": 2,
        "aggregate_status": "queued",
    }
    for task_id, dataset in zip(created.task_ids, ["dataset-b", "dataset-a"]):
        task = database.get_task(task_id)
        assert task is not None
        assert task["dataset"] == dataset
        assert task["status"] == "queued"
        assert task["stage"] is None
        assert task["created_at"] == "2026-08-20T10:00:00Z"
        task_root = settings.runtime_root / "tasks" / task_id
        assert Path(task["task_root"]) == task_root
        assert task_root.is_dir()
        assert list(task_root.iterdir()) == []


@pytest.mark.parametrize(
    ("datasets", "message"),
    [
        ([], "at least one dataset"),
        (["same", "same"], "duplicate"),
        ([f"dataset-{index}" for index in range(501)], "500"),
    ],
)
def test_invalid_dataset_lists_create_nothing(
    tmp_path: Path,
    datasets: list[str],
    message: str,
) -> None:
    settings = make_settings(tmp_path)
    database = make_database(settings.database_path)
    service = TaskService(database, settings)

    with pytest.raises(ValueError, match=message):
        service.create_task_group(datasets=datasets)

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks").exists()


def test_all_datasets_are_validated_before_any_write(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "valid")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("unused-group", "unused-task"),
    )

    with pytest.raises(ValueError, match="unknown or invalid dataset"):
        service.create_task_group(datasets=["valid", "missing"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks").exists()


@pytest.mark.parametrize("unsafe_result", ["done-file", "result-symlink"])
def test_completed_or_fail_closed_dataset_is_rejected_before_writing(
    tmp_path: Path,
    make_dataset: MakeDataset,
    unsafe_result: str,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "ready")
    completed = make_dataset(settings.data_root, "completed")
    if unsafe_result == "done-file":
        (completed / "result").mkdir()
        (completed / "result" / ".done").write_text("done")
    else:
        outside = tmp_path / "outside-result"
        outside.mkdir()
        (completed / "result").symlink_to(outside, target_is_directory=True)
    database = make_database(settings.database_path)
    service = TaskService(database, settings)

    with pytest.raises(ValueError, match="completed"):
        service.create_task_group(datasets=["ready", "completed"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks").exists()


def test_dataset_disappearing_after_resolution_is_reported_as_value_error(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    database = make_database(settings.database_path)
    service = TaskService(database, settings)
    monkeypatch.setattr(task_service_module, "discover_datasets", lambda *args: [])

    with pytest.raises(ValueError, match="changed|invalid"):
        service.create_task_group(datasets=["dataset"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks").exists()


def test_dataset_becoming_completed_before_database_insert_rolls_back(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    dataset = make_dataset(settings.data_root, "dataset")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "task"),
    )
    real_discover = task_service_module.discover_datasets
    calls = 0

    def complete_before_second_check(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            (dataset / "result").mkdir()
            (dataset / "result" / ".done").write_text("done")
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(
        task_service_module,
        "discover_datasets",
        complete_before_second_check,
    )

    with pytest.raises(ValueError, match="completed"):
        service.create_task_group(datasets=["dataset"])

    assert calls == 2
    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks" / "task").exists()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_busy_dataset_is_rejected_before_writing(
    tmp_path: Path,
    make_dataset: MakeDataset,
    status: str,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "ready")
    make_dataset(settings.data_root, "busy")
    database = make_database(settings.database_path)
    add_task(database, "busy", status=status)
    service = TaskService(database, settings)

    with pytest.raises(ValueError, match="busy|active"):
        service.create_task_group(datasets=["ready", "busy"])

    assert database.list_tasks(dataset="ready") == []
    assert not (settings.runtime_root / "tasks").exists()


def test_concurrent_active_conflict_rolls_back_whole_group_and_task_directories(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-a")
    make_dataset(settings.data_root, "dataset-b")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("new-group", "new-task-a", "new-task-b"),
        now_factory=lambda: "2026-08-20T10:00:00Z",
    )
    original_create = database.create_task_group

    def race_create(group: dict[str, object], tasks: list[dict[str, object]]) -> None:
        original_create(
            {"id": "racing-group", "created_at": "2026-08-20T09:00:00Z"},
            [task_record("racing-task", "dataset-b", group_id="racing-group")],
        )
        original_create(group, tasks)

    monkeypatch.setattr(database, "create_task_group", race_create)

    with pytest.raises(ValueError, match="active|conflict"):
        service.create_task_group(datasets=["dataset-a", "dataset-b"])

    assert database.get_task_group("new-group") is None
    assert database.get_task("new-task-a") is None
    assert database.get_task("new-task-b") is None
    assert database.get_task("racing-task") is not None
    assert not (settings.runtime_root / "tasks" / "new-task-a").exists()
    assert not (settings.runtime_root / "tasks" / "new-task-b").exists()


@pytest.mark.parametrize("conflict", ["group-id", "task-id", "task-root"])
def test_non_dataset_integrity_errors_are_rolled_back_and_propagated(
    tmp_path: Path,
    make_dataset: MakeDataset,
    conflict: str,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    database = make_database(settings.database_path)
    group_id = "new-group"
    task_id = "new-task"
    existing_group_id = "existing-group"
    existing_task_id = "existing-task"
    existing_task_root: str | None = None
    if conflict == "group-id":
        group_id = existing_group_id
    elif conflict == "task-id":
        task_id = existing_task_id
    else:
        existing_task_root = str(settings.runtime_root / "tasks" / task_id)
    database.create_task_group(
        {"id": existing_group_id, "created_at": "2026-08-20T08:00:00Z"},
        [
            task_record(
                existing_task_id,
                "old-dataset",
                group_id=existing_group_id,
                status="succeeded",
                task_root=existing_task_root,
            )
        ],
    )
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids(group_id, task_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        service.create_task_group(datasets=["dataset"])

    assert database.list_tasks(dataset="dataset") == []
    assert not (settings.runtime_root / "tasks" / task_id).exists()
    assert database.get_task(existing_task_id) is not None


def test_task_root_rejects_non_child_task_identifier(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "../escaped-task"),
    )

    with pytest.raises(ValueError, match="task identifier"):
        service.create_task_group(datasets=["dataset"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "escaped-task").exists()


@pytest.mark.parametrize("linked_component", ["runtime", "tasks"])
def test_runtime_path_symlink_is_rejected_without_writing_outside(
    tmp_path: Path,
    make_dataset: MakeDataset,
    linked_component: str,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    outside = tmp_path / "outside"
    outside.mkdir()
    if linked_component == "runtime":
        settings.runtime_root.symlink_to(outside, target_is_directory=True)
    else:
        settings.runtime_root.mkdir()
        (settings.runtime_root / "tasks").symlink_to(outside, target_is_directory=True)
    database = make_database(tmp_path / "database" / "kalibr.sqlite3")
    service = TaskService(database, settings)

    with pytest.raises(ValueError, match="symbolic link"):
        service.create_task_group(datasets=["dataset"])

    assert list(outside.iterdir()) == []
    assert database.list_tasks() == []


@pytest.mark.parametrize("swapped_component", ["tasks", "runtime"])
def test_path_swap_during_final_dataset_check_is_rejected(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
    swapped_component: str,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    database = make_database(tmp_path / "database" / "kalibr.sqlite3")
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "task"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    real_discover = task_service_module.discover_datasets
    calls = 0
    moved_root = tmp_path / f"opened-{swapped_component}"

    def swap_during_second_check(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            target = (
                settings.runtime_root / "tasks"
                if swapped_component == "tasks"
                else settings.runtime_root
            )
            target.rename(moved_root)
            target.symlink_to(outside, target_is_directory=True)
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(task_service_module, "discover_datasets", swap_during_second_check)

    with pytest.raises(ValueError, match="task path|runtime root"):
        service.create_task_group(datasets=["dataset"])

    assert calls == 2
    assert database.list_tasks() == []
    assert list(outside.iterdir()) == []
    opened_tasks = moved_root if swapped_component == "tasks" else moved_root / "tasks"
    assert list(opened_tasks.iterdir()) == []


def test_tasks_symlink_swap_cannot_redirect_creation_or_rollback(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "task"),
    )
    tasks_root = settings.runtime_root / "tasks"
    original_tasks_root = settings.runtime_root / "opened-tasks"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir
    swapped = False

    def swap_before_task_creation(
        path: str | bytes | Path,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and (Path(path).name == "task"):
            swapped = True
            tasks_root.rename(original_tasks_root)
            tasks_root.symlink_to(outside, target_is_directory=True)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_before_task_creation)

    with pytest.raises(ValueError, match="task path"):
        service.create_task_group(datasets=["dataset"])

    assert database.list_tasks() == []
    assert list(outside.iterdir()) == []
    assert list(original_tasks_root.iterdir()) == []


def test_directory_creation_failure_removes_only_roots_created_by_this_call(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset-a")
    make_dataset(settings.data_root, "dataset-b")
    preserved = settings.runtime_root / "tasks" / "task-b" / "keep.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "task-a", "task-b"),
    )

    with pytest.raises(FileExistsError):
        service.create_task_group(datasets=["dataset-a", "dataset-b"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks" / "task-a").exists()
    assert preserved.read_text() == "keep"


def test_database_failure_removes_only_new_task_directories(
    tmp_path: Path,
    make_dataset: MakeDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    make_dataset(settings.data_root, "dataset")
    preserved = settings.runtime_root / "tasks" / "existing" / "keep.txt"
    preserved.parent.mkdir(parents=True)
    preserved.write_text("keep")
    database = make_database(settings.database_path)
    service = TaskService(
        database,
        settings,
        id_factory=sequential_ids("group", "new-task"),
    )

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(database, "create_task_group", fail_insert)

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        service.create_task_group(datasets=["dataset"])

    assert database.list_tasks() == []
    assert not (settings.runtime_root / "tasks" / "new-task").exists()
    assert preserved.read_text() == "keep"
