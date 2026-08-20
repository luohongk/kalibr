from __future__ import annotations

import codecs
import errno
import os
import select
import signal
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .discovery import RESULT_FILES, resolve_dataset
from .settings import Settings


_STAGE_MARKERS = (
    ("步骤1", "imu_calibration"),
    ("步骤2a", "bag_conversion"),
    ("步骤2b", "bag_conversion"),
    ("步骤3", "camera_calibration"),
    ("步骤4", "imu_camera_calibration"),
    ("步骤5", "summarizing"),
    ("拷贝结果到", "publishing"),
)
_SKIP_MARKER = "已存在 result/.done, 跳过"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LOG_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
    | os.O_CLOEXEC
)


@dataclass(frozen=True)
class _ActiveProcess:
    process: subprocess.Popen[str]
    pgid: int


@dataclass(frozen=True)
class RunnerResult:
    exit_code: int
    interrupted: bool = False
    error_summary: str | None = None
    success: bool = False

    @property
    def succeeded(self) -> bool:
        return self.success and self.exit_code == 0 and not self.interrupted


def build_runner_command(
    settings: Settings,
    task: Mapping[str, Any],
    *,
    runner_script: Path | None = None,
) -> list[str]:
    return [
        "bash",
        str(runner_script or settings.runner_script),
        str(task["dataset"]),
    ]


class Runner:
    def __init__(
        self,
        settings: Settings,
        *,
        runner_script: Path | None = None,
        container: str | None = None,
    ) -> None:
        self.settings = settings
        self.runner_script = runner_script
        self.container = container or os.environ.get("CONTAINER", "kalibr_work")
        self._lock = threading.Lock()
        self._active: dict[str, _ActiveProcess] = {}
        self._paused: set[str] = set()
        self._interrupted: set[str] = set()
        self._termination_errors: dict[str, str] = {}
        self._terminating = False

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def run(
        self,
        task: Mapping[str, Any],
        on_stage: Callable[[str], None],
    ) -> RunnerResult:
        task_id = str(task["id"])
        task_root = Path(task["task_root"])
        recent_lines: deque[str] = deque(maxlen=20)
        process: subprocess.Popen[str] | None = None
        execution_error: str | None = None
        exit_code = 127
        skipped = False

        try:
            dataset = resolve_dataset(self.settings.data_root, str(task["dataset"]))
        except (OSError, ValueError) as exc:
            return RunnerResult(
                exit_code=127,
                error_summary=f"dataset validation failed: {exc}",
            )

        try:
            log_file = self._open_task_log(task_id, task_root)
        except OSError as exc:
            return RunnerResult(
                exit_code=127,
                error_summary=f"task filesystem validation failed: {exc}",
            )

        try:
            with log_file:
                with self._lock:
                    if self._terminating:
                        return RunnerResult(
                            exit_code=-15,
                            interrupted=True,
                            error_summary="runner is shutting down",
                        )
                    preflight_error = self._preflight_result_error(dataset)
                    if preflight_error is not None:
                        return RunnerResult(exit_code=1, error_summary=preflight_error)
                    environment = os.environ.copy()
                    environment["DATA_ROOT"] = str(self.settings.data_root)
                    process = subprocess.Popen(
                        build_runner_command(
                            self.settings,
                            task,
                            runner_script=self.runner_script,
                        ),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        errors="replace",
                        bufsize=1,
                        start_new_session=True,
                        shell=False,
                        env=environment,
                    )
                    try:
                        pgid = os.getpgid(process.pid)
                    except ProcessLookupError:
                        pgid = process.pid
                    active_process = _ActiveProcess(process=process, pgid=pgid)
                    self._active[task_id] = active_process

                try:
                    exit_code, skipped, execution_error = self._consume_output(
                        task_id,
                        active_process,
                        log_file,
                        recent_lines,
                        on_stage,
                    )
                except Exception as exc:
                    execution_error = f"runner execution error: {exc}"
                    termination_error = self._terminate_process(active_process)
                    if termination_error is not None:
                        execution_error = f"{execution_error}; {termination_error}"
                    polled_exit = process.poll()
                    exit_code = polled_exit if polled_exit is not None else -9
        except OSError as exc:
            execution_error = f"could not start runner: {exc}"
        finally:
            with self._lock:
                self._active.pop(task_id, None)
                was_paused = task_id in self._paused
                self._paused.discard(task_id)
                interrupted = task_id in self._interrupted
                self._interrupted.discard(task_id)
                termination_error = self._termination_errors.pop(task_id, None)
            if was_paused:
                self._set_container_paused(False, ignore_errors=True)

        if termination_error is not None:
            return RunnerResult(
                exit_code=exit_code,
                interrupted=interrupted,
                error_summary=termination_error,
            )

        if interrupted:
            return RunnerResult(
                exit_code=exit_code,
                interrupted=True,
                error_summary="task interrupted during shutdown",
            )
        if skipped:
            return RunnerResult(
                exit_code=exit_code,
                error_summary="runner reported existing result/.done and skipped",
            )
        if execution_error is not None:
            return RunnerResult(exit_code=exit_code, error_summary=execution_error)
        if exit_code != 0:
            return RunnerResult(
                exit_code=exit_code,
                error_summary="\n".join(recent_lines)
                or f"runner exited with code {exit_code}",
            )

        validation_error = self._result_validation_error(dataset)
        if validation_error is not None:
            return RunnerResult(exit_code=exit_code, error_summary=validation_error)
        return RunnerResult(exit_code=exit_code, success=True)

    def _open_task_log(self, task_id: str, task_root: Path) -> Any:
        runtime_root = self.settings.runtime_root
        expected_task_root = runtime_root / "tasks" / task_id
        if (
            not runtime_root.is_absolute()
            or not task_root.is_absolute()
            or task_root != expected_task_root
            or expected_task_root != Path(os.path.abspath(expected_task_root))
        ):
            raise OSError(errno.EINVAL, "task_root is not the expected canonical path")

        runtime_fd = os.open(runtime_root, _DIRECTORY_FLAGS)
        try:
            tasks_fd = os.open("tasks", _DIRECTORY_FLAGS, dir_fd=runtime_fd)
            try:
                task_fd = os.open(task_id, _DIRECTORY_FLAGS, dir_fd=tasks_fd)
                try:
                    log_fd = os.open(
                        "runner.log",
                        _LOG_FLAGS,
                        0o600,
                        dir_fd=task_fd,
                    )
                    try:
                        if not stat.S_ISREG(os.fstat(log_fd).st_mode):
                            raise OSError(errno.EINVAL, "runner.log is not a regular file")
                        return os.fdopen(log_fd, "w", encoding="utf-8")
                    except Exception:
                        os.close(log_fd)
                        raise
                finally:
                    os.close(task_fd)
            finally:
                os.close(tasks_fd)
        finally:
            os.close(runtime_fd)

    def _consume_output(
        self,
        task_id: str,
        active: _ActiveProcess,
        log_file: Any,
        recent_lines: deque[str],
        on_stage: Callable[[str], None],
    ) -> tuple[int, bool, str | None]:
        process = active.process
        assert process.stdout is not None
        output_fd = process.stdout.fileno()
        os.set_blocking(output_fd, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        skipped = False

        def handle_text(text: str, *, final: bool = False) -> None:
            nonlocal pending, skipped
            pending += text
            lines = pending.splitlines(keepends=True)
            pending = ""
            if lines and not final and not lines[-1].endswith(("\n", "\r")):
                pending = lines.pop()
            for line in lines:
                log_file.write(line)
                log_file.flush()
                stripped = line.strip()
                if stripped:
                    recent_lines.append(stripped)
                if _SKIP_MARKER in line:
                    skipped = True
                for marker, stage in _STAGE_MARKERS:
                    if marker in line:
                        try:
                            on_stage(stage)
                        except Exception:
                            pass
                        break

        try:
            while True:
                with self._lock:
                    termination_error = self._termination_errors.get(task_id)
                if termination_error is not None:
                    polled_exit = process.poll()
                    return (
                        polled_exit if polled_exit is not None else -9,
                        skipped,
                        termination_error,
                    )

                readable, _, _ = select.select([output_fd], [], [], 0.05)
                if not readable:
                    process.poll()
                    continue
                try:
                    chunk = os.read(output_fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    handle_text(decoder.decode(b"", final=True), final=True)
                    break
                handle_text(decoder.decode(chunk))
        finally:
            process.stdout.close()

        exit_code = process.wait(timeout=self.settings.terminate_grace_sec)
        return exit_code, skipped, None

    @staticmethod
    def _preflight_result_error(dataset: Path) -> str | None:
        try:
            dataset_fd = os.open(dataset, _DIRECTORY_FLAGS)
        except OSError as exc:
            return f"unsafe dataset directory: {exc}"
        try:
            try:
                result_fd = os.open("result", _DIRECTORY_FLAGS, dir_fd=dataset_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                return f"unsafe result path: {exc}"
            try:
                try:
                    done_fd = os.open(".done", _READ_FILE_FLAGS, dir_fd=result_fd)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    return f"unsafe result/.done: {exc}"
                try:
                    if not stat.S_ISREG(os.fstat(done_fd).st_mode):
                        return "unsafe result/.done: not a regular file"
                finally:
                    os.close(done_fd)
                return "result/.done already exists; refusing to start"
            finally:
                os.close(result_fd)
        finally:
            os.close(dataset_fd)

    @staticmethod
    def _result_validation_error(dataset: Path) -> str | None:
        try:
            dataset_fd = os.open(dataset, _DIRECTORY_FLAGS)
        except OSError as exc:
            return f"result dataset directory is missing or unsafe: {exc}"
        try:
            try:
                result_fd = os.open("result", _DIRECTORY_FLAGS, dir_fd=dataset_fd)
            except OSError as exc:
                return f"result directory is missing or unsafe: {exc}"
            try:
                entries = ((".done", "result/.done"),) + tuple(
                    (name, f"result file {name}") for name in RESULT_FILES
                )
                for name, label in entries:
                    try:
                        file_fd = os.open(name, _READ_FILE_FLAGS, dir_fd=result_fd)
                    except OSError as exc:
                        return f"{label} is missing or unsafe: {exc}"
                    try:
                        file_stat = os.fstat(file_fd)
                        if not stat.S_ISREG(file_stat.st_mode):
                            return f"{label} is missing or unsafe: not a regular file"
                        if file_stat.st_size == 0:
                            return f"{label} is empty"
                    finally:
                        os.close(file_fd)
            finally:
                os.close(result_fd)
        finally:
            os.close(dataset_fd)
        return None

    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_for_process_group_exit(self, active: _ActiveProcess) -> bool:
        deadline = time.monotonic() + self.settings.terminate_grace_sec
        while True:
            active.process.poll()
            if not self._process_group_exists(active.pgid):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _terminate_process(self, active: _ActiveProcess) -> str | None:
        self._set_container_paused(False, ignore_errors=True)
        try:
            os.killpg(active.pgid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.killpg(active.pgid, signal.SIGTERM)
        except ProcessLookupError:
            active.process.poll()
            return None
        except PermissionError as exc:
            return f"could not send SIGTERM to process group {active.pgid}: {exc}"
        if self._wait_for_process_group_exit(active):
            return None

        try:
            os.killpg(active.pgid, signal.SIGKILL)
        except ProcessLookupError:
            active.process.poll()
            return None
        except PermissionError as exc:
            return f"could not send SIGKILL to process group {active.pgid}: {exc}"
        if self._wait_for_process_group_exit(active):
            return None
        return f"process group {active.pgid} survived SIGKILL"

    def _set_container_paused(self, paused: bool, *, ignore_errors: bool = False) -> bool:
        action = "pause" if paused else "unpause"
        try:
            completed = subprocess.run(
                ["docker", action, self.container],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=self.settings.terminate_grace_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if ignore_errors:
                return False
            raise RuntimeError(f"docker {action} failed: {exc}") from exc
        if completed.returncode != 0:
            if ignore_errors:
                return False
            detail = completed.stdout.strip() or f"exit code {completed.returncode}"
            raise RuntimeError(f"docker {action} failed: {detail}")
        return True

    def pause(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
            if active is None or task_id in self._paused:
                return False
            self._set_container_paused(True)
            try:
                os.killpg(active.pgid, signal.SIGSTOP)
            except ProcessLookupError:
                self._set_container_paused(False, ignore_errors=True)
                return False
            except PermissionError as exc:
                self._set_container_paused(False, ignore_errors=True)
                raise RuntimeError(f"could not pause process group {active.pgid}: {exc}") from exc
            self._paused.add(task_id)
            return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            active = self._active.get(task_id)
            if active is None or task_id not in self._paused:
                return False
            self._set_container_paused(False)
            try:
                os.killpg(active.pgid, signal.SIGCONT)
            except ProcessLookupError:
                self._paused.discard(task_id)
                return False
            except PermissionError as exc:
                raise RuntimeError(f"could not resume process group {active.pgid}: {exc}") from exc
            self._paused.discard(task_id)
            return True

    def terminate_all(self) -> None:
        with self._lock:
            self._terminating = True
            active = list(self._active.items())
            self._interrupted.update(task_id for task_id, _ in active)

        for task_id, active_process in active:
            termination_error = self._terminate_process(active_process)
            if termination_error is not None:
                with self._lock:
                    if task_id in self._active:
                        self._termination_errors[task_id] = termination_error
