from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .discovery import resolve_dataset


router = APIRouter(prefix="/api/v1")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ResetError(RuntimeError):
    pass


def _entry_names(directory_fd: int) -> list[str]:
    with os.scandir(directory_fd) as entries:
        return [entry.name for entry in entries]


def _validate_tree(directory_fd: int) -> None:
    for name in _entry_names(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                _validate_tree(child_fd)
            finally:
                os.close(child_fd)
            continue
        raise ResetError(f"result contains unsafe entry: {name}")


def _delete_tree(directory_fd: int) -> None:
    for name in _entry_names(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                _delete_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def reset_dataset_result(data_root: Path, dataset_name: str) -> bool:
    dataset = resolve_dataset(data_root, dataset_name)
    dataset_fd = os.open(dataset, _DIRECTORY_FLAGS)
    try:
        try:
            result_fd = os.open("result", _DIRECTORY_FLAGS, dir_fd=dataset_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ResetError("result directory is unsafe") from exc
        try:
            _validate_tree(result_fd)
            _delete_tree(result_fd)
        finally:
            os.close(result_fd)
        os.rmdir("result", dir_fd=dataset_fd)
        return True
    finally:
        os.close(dataset_fd)


@router.delete("/datasets/{dataset_name}/result")
def reset_result(dataset_name: str, request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    database = request.app.state.database
    for task_status in ("queued", "running"):
        if database.list_tasks(
            status=task_status,
            dataset=dataset_name,
            limit=1,
        ):
            raise HTTPException(
                status_code=409,
                detail="dataset has an active task and cannot be reset",
            )
    try:
        removed = reset_dataset_result(settings.data_root, dataset_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="could not remove result directory") from exc
    return {"dataset": dataset_name, "reset": removed}
