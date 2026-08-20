from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .discovery import discover_datasets
from .events import log_file_size, stream_task_events
from .results import ResultError, list_artifacts, read_summary, resolve_artifact


class TaskGroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datasets: list[str] = Field(min_length=1, max_length=500)


class TaskGroupCreateResponse(BaseModel):
    group_id: str
    task_ids: list[str]


router = APIRouter(prefix="/api/v1")
PaginationLimit = Annotated[int, Query(ge=1, le=500)]
PaginationOffset = Annotated[int, Query(ge=0)]
TaskStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]


def _components(request: Request):
    return (
        request.app.state.settings,
        request.app.state.database,
        request.app.state.task_service,
        request.app.state.scheduler,
    )


def _public_task(task: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in task.items() if key != "task_root"}


def _interrupt_rejected_tasks(database, task_ids: list[str], reason: str) -> None:
    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for task_id in task_ids:
        task = database.get_task(task_id)
        if task is not None and task["status"] == "queued":
            database.transition_task(
                task_id,
                "interrupted",
                finished_at=finished_at,
                error_summary=reason,
            )


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    settings, _, _, _ = _components(request)
    checks = {
        "data_root": settings.data_root.is_dir(),
        "runner_script": settings.runner_script.is_file(),
        "frontend_dist": (settings.frontend_dist / "index.html").is_file(),
    }
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@router.get("/datasets")
def datasets(request: Request) -> dict[str, object]:
    settings, database, _, _ = _components(request)
    active: dict[str, str] = {}
    for task_status in ("queued", "running"):
        for task in database.list_tasks(status=task_status, limit=500):
            active[str(task["dataset"])] = str(task["id"])
    return {"items": discover_datasets(settings.data_root, active)}


@router.post(
    "/task-groups",
    response_model=TaskGroupCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task_group(
    body: TaskGroupCreateRequest,
    request: Request,
) -> TaskGroupCreateResponse:
    _, database, service, scheduler = _components(request)
    try:
        created = service.create_task_group(datasets=body.datasets)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="task storage is unavailable") from exc
    try:
        scheduler.enqueue(created.task_ids)
    except (KeyError, ValueError, RuntimeError) as exc:
        _interrupt_rejected_tasks(database, created.task_ids, f"scheduler rejected task: {exc}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return TaskGroupCreateResponse(group_id=created.group_id, task_ids=created.task_ids)


@router.get("/task-groups")
def task_groups(
    request: Request,
    limit: PaginationLimit = 50,
    offset: PaginationOffset = 0,
) -> dict[str, object]:
    _, database, _, _ = _components(request)
    return {"items": database.list_task_groups(limit=limit, offset=offset)}


@router.get("/task-groups/{group_id}")
def task_group(group_id: str, request: Request) -> dict[str, object]:
    _, database, _, _ = _components(request)
    group = database.get_task_group(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="task group not found")
    group["tasks"] = [
        _public_task(item)
        for item in database.list_tasks(group_id=group_id, limit=500)
    ]
    return group


@router.get("/tasks")
def tasks(
    request: Request,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    dataset: str | None = None,
    group_id: str | None = None,
    limit: PaginationLimit = 50,
    offset: PaginationOffset = 0,
) -> dict[str, object]:
    _, database, _, _ = _components(request)
    items = database.list_tasks(
        status=status_filter,
        dataset=dataset,
        group_id=group_id,
        limit=limit,
        offset=offset,
    )
    return {"items": [_public_task(item) for item in items]}


@router.get("/tasks/{task_id}")
def task(task_id: str, request: Request) -> dict[str, object]:
    _, database, _, _ = _components(request)
    item = database.get_task_details(task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="task not found")
    return _public_task(item)


@router.get("/tasks/{task_id}/events")
def task_events(
    task_id: str,
    request: Request,
    offset: Annotated[str, Query()] = "0",
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    _, database, _, _ = _components(request)
    task_item = database.get_task_details(task_id)
    if task_item is None:
        raise HTTPException(status_code=404, detail="task not found")
    raw_offset = last_event_id if last_event_id is not None else offset
    try:
        offset_value = int(raw_offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid log offset") from exc
    if offset_value < 0:
        raise HTTPException(status_code=400, detail="invalid log offset")
    log_path = Path(str(task_item["task_root"])) / "runner.log"
    if offset_value > log_file_size(log_path):
        raise HTTPException(status_code=400, detail="log offset exceeds file size")
    return StreamingResponse(
        stream_task_events(database, task_id, offset=offset_value),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/summary")
def task_summary(task_id: str, request: Request) -> dict[str, str | None]:
    settings, database, _, _ = _components(request)
    task_item = database.get_task_details(task_id)
    if task_item is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        return read_summary(task_item, settings.data_root)
    except ResultError as exc:
        code = 404 if exc.code == "summary_missing" else 422
        raise HTTPException(
            status_code=code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/tasks/{task_id}/artifacts")
def task_artifacts(task_id: str, request: Request) -> dict[str, object]:
    settings, database, _, _ = _components(request)
    task_item = database.get_task_details(task_id)
    if task_item is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"items": list_artifacts(task_item, settings.data_root)}


@router.get("/tasks/{task_id}/artifacts/{artifact_path:path}")
def download_task_artifact(
    task_id: str,
    artifact_path: str,
    request: Request,
) -> FileResponse:
    settings, database, _, _ = _components(request)
    task_item = database.get_task_details(task_id)
    if task_item is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        artifact = resolve_artifact(task_item, settings.data_root, artifact_path)
    except ResultError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(artifact, filename=artifact.name)


@router.get("/runtime")
def runtime(request: Request) -> dict[str, int | bool]:
    _, _, _, scheduler = _components(request)
    return scheduler.runtime_summary()
