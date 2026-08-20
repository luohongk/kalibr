from __future__ import annotations

import errno
import os
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .db import Database
from .discovery import discover_datasets, resolve_dataset
from .settings import Settings


@dataclass(frozen=True)
class CreatedTaskGroup:
    group_id: str
    task_ids: list[str]


def _new_id() -> str:
    return str(uuid4())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        id_factory: Callable[[], str] = _new_id,
        now_factory: Callable[[], str] = _utc_now,
    ) -> None:
        self.database = database
        self.settings = settings
        self.id_factory = id_factory
        self.now_factory = now_factory

    def create_task_group(self, *, datasets: list[str]) -> CreatedTaskGroup:
        if not datasets:
            raise ValueError("at least one dataset is required")
        if len(datasets) > 500:
            raise ValueError("at most 500 datasets are allowed")
        if len(datasets) != len(set(datasets)):
            raise ValueError("duplicate datasets are not allowed")

        for dataset in datasets:
            resolve_dataset(self.settings.data_root, dataset)

        self._validate_dataset_states(datasets)

        group_id = self.id_factory()
        created_at = self.now_factory()
        task_ids: list[str] = []
        task_records: list[dict[str, object]] = []
        created_names: list[str] = []
        tasks_root, runtime_fd, tasks_fd = self._open_tasks_root()

        try:
            for dataset in datasets:
                task_id = self.id_factory()
                task_root = self._task_root(tasks_root, task_id)
                os.mkdir(task_id, dir_fd=tasks_fd)
                created_names.append(task_id)
                task_ids.append(task_id)
                task_records.append(
                    {
                        "id": task_id,
                        "group_id": group_id,
                        "dataset": dataset,
                        "status": "queued",
                        "stage": None,
                        "created_at": created_at,
                        "task_root": str(task_root),
                    }
                )

            self._validate_dataset_states(datasets)
            self._verify_open_task_path(tasks_root.parent, runtime_fd, tasks_fd)
            self.database.create_task_group(
                {"id": group_id, "created_at": created_at},
                task_records,
            )
        except sqlite3.IntegrityError as exc:
            self._remove_created_roots(tasks_fd, created_names)
            if str(exc) == "UNIQUE constraint failed: tasks.dataset":
                raise ValueError(
                    "task group conflicts with an existing active task"
                ) from exc
            raise
        except Exception:
            self._remove_created_roots(tasks_fd, created_names)
            raise
        finally:
            os.close(tasks_fd)
            os.close(runtime_fd)

        return CreatedTaskGroup(group_id=group_id, task_ids=task_ids)

    def _validate_dataset_states(self, datasets: list[str]) -> None:
        active_tasks: dict[str, str] = {}
        for dataset in datasets:
            for status in ("queued", "running"):
                matches = self.database.list_tasks(
                    status=status,
                    dataset=dataset,
                    limit=1,
                )
                if matches:
                    active_tasks[dataset] = str(matches[0]["id"])
                    break

        try:
            states = {
                item.name: item.state
                for item in discover_datasets(self.settings.data_root, active_tasks)
            }
        except OSError as exc:
            raise ValueError("dataset files changed during validation") from exc
        for dataset in datasets:
            state = states.get(dataset)
            if state is None:
                raise ValueError(f"dataset became invalid during validation: {dataset}")
            if state == "completed":
                raise ValueError(f"dataset is already completed: {dataset}")
            if state == "busy":
                raise ValueError(f"dataset already has an active task: {dataset}")

    def _open_tasks_root(self) -> tuple[Path, int, int]:
        runtime_root = self.settings.runtime_root
        if runtime_root.is_symlink():
            raise ValueError("runtime root cannot be a symbolic link")
        runtime_root.mkdir(parents=True, exist_ok=True)

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            runtime_fd = os.open(runtime_root, flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("runtime root cannot be a symbolic link") from exc
            raise

        try:
            try:
                os.mkdir("tasks", dir_fd=runtime_fd)
            except FileExistsError:
                pass
            try:
                tasks_fd = os.open("tasks", flags, dir_fd=runtime_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("task path cannot contain a symbolic link") from exc
                raise
        except Exception:
            os.close(runtime_fd)
            raise

        return runtime_root / "tasks", runtime_fd, tasks_fd

    @staticmethod
    def _verify_open_task_path(
        runtime_root: Path,
        runtime_fd: int,
        tasks_fd: int,
    ) -> None:
        try:
            opened_runtime = os.fstat(runtime_fd)
            current_runtime = os.stat(runtime_root, follow_symlinks=False)
            opened_tasks = os.fstat(tasks_fd)
            current_tasks = os.stat("tasks", dir_fd=runtime_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("runtime or task path changed during task creation") from exc

        if (
            not stat.S_ISDIR(current_runtime.st_mode)
            or (current_runtime.st_dev, current_runtime.st_ino)
            != (opened_runtime.st_dev, opened_runtime.st_ino)
        ):
            raise ValueError("runtime root changed during task creation")
        if (
            not stat.S_ISDIR(current_tasks.st_mode)
            or (current_tasks.st_dev, current_tasks.st_ino)
            != (opened_tasks.st_dev, opened_tasks.st_ino)
        ):
            raise ValueError("task path changed during task creation")

    @staticmethod
    def _task_root(tasks_root: Path, task_id: str) -> Path:
        relative = Path(task_id)
        if (
            not task_id
            or relative.is_absolute()
            or len(relative.parts) != 1
            or relative.as_posix() != task_id
            or task_id in {".", ".."}
        ):
            raise ValueError("task identifier must be a canonical direct-child name")
        return tasks_root / relative

    @staticmethod
    def _remove_created_roots(tasks_fd: int, created_names: list[str]) -> None:
        for task_id in reversed(created_names):
            try:
                os.rmdir(task_id, dir_fd=tasks_fd)
            except OSError:
                pass
