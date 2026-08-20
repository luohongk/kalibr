from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/api/v1")


def _public_task(task: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in task.items() if key != "task_root"}


def _controlled_task(request: Request, task_id: str) -> dict[str, object]:
    task = request.app.state.database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, request: Request) -> dict[str, object]:
    _controlled_task(request, task_id)
    try:
        request.app.state.scheduler.pause_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    task = request.app.state.database.get_task(task_id)
    assert task is not None
    return _public_task(task)


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, request: Request) -> dict[str, object]:
    _controlled_task(request, task_id)
    try:
        request.app.state.scheduler.resume_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    task = request.app.state.database.get_task(task_id)
    assert task is not None
    return _public_task(task)


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, request: Request) -> dict[str, object]:
    _controlled_task(request, task_id)
    try:
        deleted = request.app.state.scheduler.delete_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    settings = request.app.state.settings
    expected = settings.runtime_root / "tasks" / task_id
    task_root = Path(str(deleted["task_root"]))
    if task_root == expected and not task_root.is_symlink():
        try:
            shutil.rmtree(task_root)
        except OSError:
            pass
    return {"task_id": task_id, "deleted": True}
