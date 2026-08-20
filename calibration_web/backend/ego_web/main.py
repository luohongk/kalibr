from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .db import Database
from .reset_api import router as reset_router
from .runner import Runner
from .scheduler import Scheduler, TaskRunner
from .settings import Settings
from .task_control_api import router as task_control_router
from .task_service import TaskService


def create_app(
    *,
    settings: Settings | None = None,
    runner: TaskRunner | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    database = Database(app_settings.database_path)
    task_service = TaskService(database, app_settings)
    task_runner = runner or Runner(app_settings)
    scheduler = Scheduler(
        database,
        task_runner,
        max_concurrency=app_settings.max_concurrency,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        scheduler.recover_unfinished()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(title="Kalibr Calibration Web", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.database = database
    app.state.task_service = task_service
    app.state.scheduler = scheduler
    app.include_router(router)
    app.include_router(reset_router)
    app.include_router(task_control_router)

    assets = app_settings.frontend_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend(frontend_path: str) -> FileResponse:
        if frontend_path == "api" or frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        index = app_settings.frontend_dist / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="frontend build is missing")
        return FileResponse(index)

    return app


app = create_app()
