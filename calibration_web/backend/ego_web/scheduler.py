from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

from .db import Database, InvalidStatusTransition, TERMINAL_STATUSES
from .runner import RunnerResult


class TaskRunner(Protocol):
    def run(
        self,
        task: dict[str, Any],
        on_stage: Callable[[str], None],
    ) -> RunnerResult: ...

    def terminate_all(self) -> None: ...

    def pause(self, task_id: str) -> bool: ...

    def resume(self, task_id: str) -> bool: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Scheduler:
    def __init__(
        self,
        database: Database,
        runner: TaskRunner,
        *,
        max_concurrency: int,
        now_factory: Callable[[], str] = _utc_now,
        worker_join_timeout: float = 5.0,
    ) -> None:
        if max_concurrency != 1:
            raise ValueError("max_concurrency must be exactly 1")
        if worker_join_timeout <= 0:
            raise ValueError("worker_join_timeout must be positive")
        self.database = database
        self.runner = runner
        self.max_concurrency = max_concurrency
        self.now_factory = now_factory
        self._worker_join_timeout = worker_join_timeout
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = "new"
        self._stop_error: str | None = None
        self._running_ids: set[str] = set()
        self._paused_stages: dict[str, str | None] = {}
        self._stopping = threading.Event()
        self.accepting_tasks = False

    def recover_unfinished(self) -> int:
        return self.database.mark_unfinished_interrupted(self.now_factory())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "running":
                return
            if self._lifecycle_state != "new":
                raise RuntimeError("scheduler cannot be restarted after stop")
            worker = threading.Thread(
                target=self._worker,
                name="ego-task-worker-1",
                daemon=True,
            )
            worker.start()
            self._workers = [worker]
            self._lifecycle_state = "running"
            self._stopping.clear()
            with self._state_lock:
                self.accepting_tasks = True

    def enqueue(self, task_ids: Iterable[str]) -> None:
        validated: list[str] = []
        with self._state_lock:
            if not self.accepting_tasks:
                raise RuntimeError("scheduler is not accepting tasks")

        for task_id in task_ids:
            task = self.database.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["status"] != "queued":
                raise ValueError(f"task {task_id} is not queued")
            validated.append(task_id)

        with self._state_lock:
            if self.accepting_tasks:
                for task_id in validated:
                    self._queue.put(task_id)
                return

        for task_id in validated:
            self._interrupt_task(task_id, "task rejected during scheduler shutdown")
        raise RuntimeError("scheduler is not accepting tasks")

    def wait_until_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def pause_task(self, task_id: str) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "running" or task["stage"] == "paused":
            raise ValueError("only a running task can be paused")
        if not self.runner.pause(task_id):
            raise RuntimeError("task process is not available for pausing")
        previous_stage = task.get("stage")
        with self._state_lock:
            self._paused_stages[task_id] = (
                str(previous_stage) if previous_stage is not None else None
            )
        try:
            self.database.update_task_stage(task_id, "paused")
        except Exception:
            with self._state_lock:
                self._paused_stages.pop(task_id, None)
            self.runner.resume(task_id)
            raise

    def resume_task(self, task_id: str) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "running" or task["stage"] != "paused":
            raise ValueError("only a paused task can be resumed")
        with self._state_lock:
            previous_stage = self._paused_stages.get(task_id)
        try:
            self.database.update_task_stage(task_id, previous_stage or "preparing")
        except Exception:
            raise
        if not self.runner.resume(task_id):
            self.database.update_task_stage(task_id, "paused")
            raise RuntimeError("task process is not available for resuming")
        with self._state_lock:
            self._paused_stages.pop(task_id, None)

    def delete_task(self, task_id: str) -> dict[str, Any]:
        task = self.database.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] == "running":
            raise ValueError("a running task cannot be deleted")
        deleted = self.database.delete_inactive_task(task_id)
        if deleted is None:
            raise ValueError("task is running or no longer exists")
        return deleted

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state == "new":
                with self._state_lock:
                    self.accepting_tasks = False
                self._lifecycle_state = "stopped"
                return
            if self._lifecycle_state == "stopped":
                return
            if self._lifecycle_state == "failed":
                raise RuntimeError(self._stop_error or "scheduler stop failed")

            self._lifecycle_state = "stopping"
            with self._state_lock:
                self.accepting_tasks = False
            self._stopping.set()
            workers = list(self._workers)

            self._interrupt_queued_tasks()
            self.runner.terminate_all()
            self._queue.put(None)
            for worker in workers:
                worker.join(timeout=self._worker_join_timeout)
            alive_workers = [worker for worker in workers if worker.is_alive()]
            if alive_workers:
                self._stop_error = (
                    "scheduler worker did not stop within "
                    f"{self._worker_join_timeout:g} seconds"
                )
                self._lifecycle_state = "failed"
                raise RuntimeError(self._stop_error)

            self._workers = []
            with self._state_lock:
                self._running_ids.clear()
                self._paused_stages.clear()
            self._lifecycle_state = "stopped"

    def runtime_summary(self) -> dict[str, int | bool]:
        with self._state_lock:
            running = len(self._running_ids)
            accepting = self.accepting_tasks
        queued = len(self.database.list_tasks(status="queued", limit=500))
        return {
            "max_concurrency": self.max_concurrency,
            "running": running,
            "queued": queued,
            "accepting_tasks": accepting,
        }

    def _interrupt_queued_tasks(self) -> None:
        while True:
            try:
                task_id = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if task_id is not None:
                    self._interrupt_task(task_id, "task interrupted before start")
            finally:
                self._queue.task_done()

    def _interrupt_task(self, task_id: str, error_summary: str) -> None:
        task = self.database.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return
        try:
            self.database.transition_task(
                task_id,
                "interrupted",
                finished_at=self.now_factory(),
                error_summary=error_summary,
            )
        except InvalidStatusTransition:
            pass

    def _worker(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                self._execute(task_id)
            except Exception as exc:
                if task_id is not None:
                    try:
                        self._record_exception(task_id, exc)
                    except Exception:
                        pass
            finally:
                self._queue.task_done()

    def _execute(self, task_id: str) -> None:
        running_registered = False
        try:
            if self._stopping.is_set():
                self._interrupt_task(task_id, "task interrupted before launch")
                return

            task = self.database.get_task_details(task_id)
            if task is None or task["status"] != "queued":
                return
            self.database.transition_task(
                task_id,
                "running",
                started_at=self.now_factory(),
            )
            self.database.update_task_stage(task_id, "preparing")
            with self._state_lock:
                self._running_ids.add(task_id)
                running_registered = True

            if self._stopping.is_set():
                self._record_result(
                    task_id,
                    RunnerResult(
                        exit_code=-15,
                        interrupted=True,
                        error_summary="task interrupted before launch",
                    ),
                )
                return

            result = self.runner.run(
                task,
                lambda stage: self._update_stage(task_id, stage),
            )
            self._record_result(task_id, result)
        except Exception as exc:
            self._record_exception(task_id, exc)
        finally:
            if running_registered:
                with self._state_lock:
                    self._running_ids.discard(task_id)
                    self._paused_stages.pop(task_id, None)

    def _update_stage(self, task_id: str, stage: str) -> None:
        with self._state_lock:
            if task_id in self._paused_stages:
                return
        self.database.update_task_stage(task_id, stage)

    def _record_result(self, task_id: str, result: RunnerResult) -> None:
        task = self.database.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return
        finished_at = self.now_factory()
        if result.interrupted or self._stopping.is_set():
            self.database.transition_task(
                task_id,
                "interrupted",
                finished_at=finished_at,
                exit_code=result.exit_code,
                error_summary=result.error_summary or "task interrupted during shutdown",
            )
        elif result.succeeded:
            self.database.transition_task(
                task_id,
                "succeeded",
                finished_at=finished_at,
                exit_code=result.exit_code,
            )
        else:
            self.database.transition_task(
                task_id,
                "failed",
                finished_at=finished_at,
                exit_code=result.exit_code,
                error_summary=result.error_summary or "runner result validation failed",
            )

    def _record_exception(self, task_id: str, exc: Exception) -> None:
        task = self.database.get_task(task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return
        status = "interrupted" if self._stopping.is_set() else "failed"
        try:
            self.database.transition_task(
                task_id,
                status,
                finished_at=self.now_factory(),
                error_summary=f"task execution error: {exc}",
            )
        except InvalidStatusTransition:
            pass
