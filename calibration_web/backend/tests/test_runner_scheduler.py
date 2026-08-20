from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ego_web import runner as runner_module
from ego_web.db import Database
from ego_web.discovery import REQUIRED_BAGS, RESULT_FILES
from ego_web.runner import Runner, RunnerResult, build_runner_command
from ego_web.scheduler import Scheduler
from ego_web.settings import Settings


FAKE_RUNNER = Path(__file__).with_name("fake_runner.py")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime",
        max_concurrency=1,
        terminate_grace_sec=0.5,
    )


def create_dataset(settings: Settings, name: str) -> Path:
    dataset = settings.data_root / name
    dataset.mkdir(parents=True)
    for bag in REQUIRED_BAGS:
        (dataset / bag).write_bytes(b"bag")
    return dataset


def task_details(tmp_path: Path, dataset: str) -> dict[str, Any]:
    task_root = tmp_path / "runtime" / "tasks" / f"task-{dataset}"
    task_root.mkdir(parents=True)
    return {
        "id": f"task-{dataset}",
        "dataset": dataset,
        "status": "queued",
        "stage": None,
        "task_root": str(task_root),
    }


def make_runner(tmp_path: Path, dataset: str) -> tuple[Runner, dict[str, Any], Path]:
    settings = make_settings(tmp_path)
    dataset_path = create_dataset(settings, dataset)
    return Runner(settings, runner_script=FAKE_RUNNER), task_details(tmp_path, dataset), dataset_path


def test_build_runner_command_is_exact_and_uses_override(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    task = task_details(tmp_path, "dataset with spaces")

    assert build_runner_command(settings, task) == [
        "bash",
        str(settings.runner_script),
        "dataset with spaces",
    ]
    assert build_runner_command(settings, task, runner_script=FAKE_RUNNER) == [
        "bash",
        str(FAKE_RUNNER),
        "dataset with spaces",
    ]


def test_runner_result_success_requires_zero_exit_and_no_interruption() -> None:
    assert RunnerResult(exit_code=0, success=True).succeeded is True
    assert RunnerResult(exit_code=7, success=True).succeeded is False
    assert RunnerResult(exit_code=0, interrupted=True, success=True).succeeded is False


def test_runner_uses_inherited_environment_logs_and_reports_all_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task, dataset = make_runner(tmp_path, "success")
    monkeypatch.setenv("RUNNER_PARENT_ENV", "inherited")
    stages: list[str] = []

    result = runner.run(task, stages.append)

    assert result.exit_code == 0
    assert result.interrupted is False
    assert result.error_summary is None
    assert result.succeeded is True
    assert "runner_status" not in RunnerResult.__dataclass_fields__
    assert (dataset / "invocation.txt").read_text().splitlines() == [
        str(runner.settings.data_root),
        "success",
        "inherited",
    ]
    assert stages == [
        "imu_calibration",
        "bag_conversion",
        "bag_conversion",
        "camera_calibration",
        "imu_camera_calibration",
        "summarizing",
        "publishing",
    ]
    log = (Path(task["task_root"]) / "runner.log").read_text()
    assert "步骤1: imu_utils Allan 方差标定" in log
    assert "stderr is merged" in log
    assert "未知日志不应改变阶段" in log


def test_runner_passes_safe_popen_options_and_records_launch_pgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task, _ = make_runner(tmp_path, "popen-options")
    original_popen = subprocess.Popen
    original_getpgid = os.getpgid
    popen_calls: list[tuple[list[str], dict[str, Any]]] = []
    processes: list[subprocess.Popen[str]] = []
    pgid_lookups: list[int] = []

    def capturing_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        popen_calls.append((command, kwargs))
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    def capturing_getpgid(pid: int) -> int:
        pgid_lookups.append(pid)
        return original_getpgid(pid)

    monkeypatch.setattr(runner_module.subprocess, "Popen", capturing_popen)
    monkeypatch.setattr(runner_module.os, "getpgid", capturing_getpgid)

    result = runner.run(task, lambda stage: None)

    assert result.succeeded is True
    assert len(popen_calls) == 1
    command, kwargs = popen_calls[0]
    assert command == ["bash", str(FAKE_RUNNER), "popen-options"]
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["text"] is True
    assert kwargs["errors"] == "replace"
    assert kwargs["bufsize"] == 1
    assert kwargs["start_new_session"] is True
    assert kwargs["shell"] is False
    assert kwargs["env"]["DATA_ROOT"] == str(runner.settings.data_root)
    assert pgid_lookups == [processes[0].pid]


def test_runner_flushes_log_while_process_is_running(tmp_path: Path) -> None:
    runner, task, dataset = make_runner(tmp_path, "live-flush")
    results: list[RunnerResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run(task, lambda stage: None))
    )
    thread.start()
    blocked = dataset / "blocked"
    deadline = time.monotonic() + 3
    while not blocked.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert blocked.exists()
    log_path = Path(task["task_root"]) / "runner.log"
    assert "live output marker" in log_path.read_text()

    runner.terminate_all()
    thread.join(3)
    assert not thread.is_alive()
    assert results[0].interrupted is True


def test_stage_callback_failure_does_not_stop_runner(tmp_path: Path) -> None:
    runner, task, dataset = make_runner(tmp_path, "callback")
    callback_count = 0

    def failing_callback(stage: str) -> None:
        nonlocal callback_count
        callback_count += 1
        raise RuntimeError(f"callback failed at {stage}")

    result = runner.run(task, failing_callback)

    assert result.succeeded is True
    assert callback_count == 7
    assert (dataset / "result" / ".done").is_file()
    assert runner.active_count == 0


@pytest.mark.parametrize(
    ("dataset_name", "expected_error"),
    [
        ("no-done", "result/.done"),
        ("missing-yaml", "kalibr_input-camchain-imucam.yaml"),
        ("empty-yaml", "kalibr_input-camchain-imucam.yaml"),
        ("symlink-yaml", "kalibr_input-camchain-imucam.yaml"),
        ("done-symlink", "result/.done"),
        ("skip", "skipped"),
    ],
)
def test_runner_rejects_incomplete_unsafe_and_skipped_results(
    tmp_path: Path, dataset_name: str, expected_error: str
) -> None:
    runner, task, _ = make_runner(tmp_path, dataset_name)

    result = runner.run(task, lambda stage: None)

    assert result.exit_code == 0
    assert result.interrupted is False
    assert result.succeeded is False
    assert expected_error in (result.error_summary or "")


def test_runner_nonzero_error_summary_contains_last_twenty_nonempty_lines(
    tmp_path: Path,
) -> None:
    runner, task, _ = make_runner(tmp_path, "fail")

    result = runner.run(task, lambda stage: None)

    assert result.exit_code == 7
    assert result.succeeded is False
    assert (result.error_summary or "").splitlines() == [
        f"fake error {number}" for number in range(6, 26)
    ]


@pytest.mark.parametrize("unsafe_kind", ["done", "done-symlink", "result-symlink", "result-file"])
def test_runner_refuses_existing_or_unsafe_result_before_spawn(
    tmp_path: Path, unsafe_kind: str
) -> None:
    runner, task, dataset = make_runner(tmp_path, f"pre-{unsafe_kind}")
    result = dataset / "result"
    outside = tmp_path / "outside"
    if unsafe_kind == "result-symlink":
        outside.mkdir()
        result.symlink_to(outside, target_is_directory=True)
    elif unsafe_kind == "result-file":
        result.write_text("not a directory")
    else:
        result.mkdir()
        if unsafe_kind == "done":
            (result / ".done").write_text("done")
        else:
            outside.write_text("done")
            (result / ".done").symlink_to(outside)

    run_result = runner.run(task, lambda stage: None)

    assert run_result.succeeded is False
    assert run_result.exit_code != 0
    assert run_result.error_summary
    assert not (dataset / "invocation.txt").exists()


@pytest.mark.parametrize("unsafe_entry", ["task-root", "runner-log"])
def test_runner_refuses_symlinked_task_paths_without_touching_target(
    tmp_path: Path, unsafe_entry: str
) -> None:
    runner, task, _ = make_runner(tmp_path, f"unsafe-{unsafe_entry}")
    task_root = Path(task["task_root"])
    outside = tmp_path / "outside-task"
    outside.mkdir()
    outside_log = outside / "runner.log"
    outside_log.write_text("keep me")
    if unsafe_entry == "task-root":
        task_root.rmdir()
        task_root.symlink_to(outside, target_is_directory=True)
    else:
        (task_root / "runner.log").symlink_to(outside_log)

    result = runner.run(task, lambda stage: None)

    assert result.succeeded is False
    assert result.error_summary
    assert outside_log.read_text() == "keep me"


def test_runner_refuses_symlinked_tasks_directory_without_touching_target(
    tmp_path: Path,
) -> None:
    runner, task, dataset = make_runner(tmp_path, "unsafe-tasks-directory")
    tasks_root = runner.settings.runtime_root / "tasks"
    tasks_root.rename(runner.settings.runtime_root / "real-tasks")
    outside_tasks = tmp_path / "outside-tasks"
    outside_task = outside_tasks / str(task["id"])
    outside_task.mkdir(parents=True)
    outside_log = outside_task / "runner.log"
    outside_log.write_text("keep me")
    tasks_root.symlink_to(outside_tasks, target_is_directory=True)

    result = runner.run(task, lambda stage: None)

    assert result.succeeded is False
    assert result.error_summary
    assert outside_log.read_text() == "keep me"
    assert not (dataset / "invocation.txt").exists()


@pytest.mark.parametrize("race_target", ["result", ".done", RESULT_FILES[0]])
def test_runner_result_validation_rejects_symlink_swap_at_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_target: str,
) -> None:
    runner, task, dataset = make_runner(tmp_path, f"race-{race_target}")
    original_open = os.open
    swapped = False
    outside = tmp_path / f"outside-{race_target.replace('.', 'dot')}"

    def racing_open(path: str | bytes, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        relative_path = os.fsdecode(path)
        result = dataset / "result"
        if not swapped and relative_path == race_target and result.is_dir():
            if race_target == "result":
                saved_result = dataset / "saved-result"
                result.rename(saved_result)
                outside.mkdir()
                for name in RESULT_FILES:
                    (outside / name).write_text("outside")
                (outside / ".done").write_text("outside")
                result.symlink_to(outside, target_is_directory=True)
            else:
                target = result / race_target
                outside.write_text("outside")
                target.unlink()
                target.symlink_to(outside)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(runner_module.os, "open", racing_open)

    result = runner.run(task, lambda stage: None)

    assert swapped is True
    assert result.succeeded is False
    assert "unsafe" in (result.error_summary or "")
    if race_target != "result":
        assert outside.read_text() == "outside"


def test_runner_resolves_and_revalidates_dataset_before_spawn(tmp_path: Path) -> None:
    runner, task, dataset = make_runner(tmp_path, "invalidated")
    (dataset / REQUIRED_BAGS[0]).unlink()

    result = runner.run(task, lambda stage: None)

    assert result.succeeded is False
    assert "dataset" in (result.error_summary or "")
    assert not (dataset / "invocation.txt").exists()


def test_runner_terminates_the_whole_process_group(tmp_path: Path) -> None:
    runner, task, dataset = make_runner(tmp_path, "slow")
    result_holder: list[RunnerResult] = []
    started = threading.Event()
    thread = threading.Thread(
        target=lambda: result_holder.append(
            runner.run(
                task,
                lambda stage: started.set() if stage == "imu_calibration" else None,
            )
        )
    )

    thread.start()
    assert started.wait(3)
    child_pid_file = dataset / "child.pid"
    deadline = time.monotonic() + 3
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())

    runner.terminate_all()
    thread.join(3)

    assert not thread.is_alive()
    assert result_holder[0].interrupted is True
    assert result_holder[0].succeeded is False
    assert runner.active_count == 0
    deadline = time.monotonic() + 3
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not Path(f"/proc/{child_pid}").exists()


def test_runner_terminates_descendants_after_parent_exits_with_stdout_open(
    tmp_path: Path,
) -> None:
    runner, task, dataset = make_runner(tmp_path, "parent-exit-stdout")
    results: list[RunnerResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run(task, lambda stage: None))
    )
    thread.start()
    parent_exited = dataset / "parent-exited"
    child_pid_file = dataset / "child.pid"
    deadline = time.monotonic() + 3
    while (
        not parent_exited.exists() or not child_pid_file.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert parent_exited.exists()
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    time.sleep(0.05)

    runner.terminate_all()
    thread.join(1)
    stopped_without_cleanup = not thread.is_alive()
    if not stopped_without_cleanup:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        thread.join(3)

    assert stopped_without_cleanup
    assert results[0].interrupted is True
    deadline = time.monotonic() + 3
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not Path(f"/proc/{child_pid}").exists()


def test_runner_reports_process_group_surviving_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task, dataset = make_runner(tmp_path, "ignore-term")
    runner.settings.terminate_grace_sec = 0.05
    results: list[RunnerResult] = []
    started = threading.Event()
    thread = threading.Thread(
        target=lambda: results.append(
            runner.run(
                task,
                lambda stage: started.set() if stage == "imu_calibration" else None,
            )
        )
    )
    thread.start()
    assert started.wait(3)
    child_pid_file = dataset / "child.pid"
    deadline = time.monotonic() + 3
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    pgid = os.getpgid(child_pid)
    original_killpg = os.killpg

    def ignore_sigkill(process_group: int, sig: int) -> None:
        if sig != signal.SIGKILL:
            original_killpg(process_group, sig)

    monkeypatch.setattr(runner_module.os, "killpg", ignore_sigkill)
    runner.terminate_all()
    thread.join(1)
    completed_with_failure = not thread.is_alive()
    try:
        original_killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    thread.join(3)

    assert completed_with_failure
    assert results[0].interrupted is True
    assert "survived SIGKILL" in (results[0].error_summary or "")


def test_shutdown_racing_with_validation_cannot_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task, dataset = make_runner(tmp_path, "race")
    validation_started = threading.Event()
    release_validation = threading.Event()
    original_resolve = runner_module.resolve_dataset

    def blocked_resolve(data_root: Path, dataset_name: str) -> Path:
        validation_started.set()
        assert release_validation.wait(3)
        return original_resolve(data_root, dataset_name)

    monkeypatch.setattr(runner_module, "resolve_dataset", blocked_resolve)
    results: list[RunnerResult] = []
    thread = threading.Thread(target=lambda: results.append(runner.run(task, lambda stage: None)))
    thread.start()
    assert validation_started.wait(3)

    runner.terminate_all()
    release_validation.set()
    thread.join(3)

    assert not thread.is_alive()
    assert results[0].interrupted is True
    assert results[0].succeeded is False
    assert not (dataset / "invocation.txt").exists()


def create_database_with_tasks(tmp_path: Path, datasets: list[str]) -> Database:
    database = Database(tmp_path / "platform.sqlite3")
    database.initialize()
    database.create_task_group(
        {"id": "group", "created_at": "2026-08-20T10:00:00Z"},
        [
            {
                "id": f"task-{dataset}",
                "group_id": "group",
                "dataset": dataset,
                "status": "queued",
                "created_at": f"2026-08-20T10:00:{index:02d}Z",
                "task_root": str(tmp_path / "tasks" / f"task-{dataset}"),
            }
            for index, dataset in enumerate(datasets)
        ],
    )
    return database


class ControlledRunner:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.entry_states: list[tuple[str, str, str | None]] = []
        self.stage_states: list[tuple[str, str, str | None]] = []
        self.active = 0
        self.max_active = 0
        self.started = threading.Event()
        self.terminated = threading.Event()

    def run(self, task: dict[str, Any], on_stage: Callable[[str], None]) -> RunnerResult:
        dataset = str(task["dataset"])
        with self.lock:
            self.order.append(dataset)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.database is not None:
                current = self.database.get_task(str(task["id"]))
                assert current is not None
                self.entry_states.append((dataset, str(current["status"]), current["stage"]))
            on_stage("camera_calibration")
            if self.database is not None:
                current = self.database.get_task(str(task["id"]))
                assert current is not None
                self.stage_states.append((dataset, str(current["status"]), current["stage"]))
            if dataset == "slow":
                self.started.set()
                assert self.terminated.wait(5)
                return RunnerResult(
                    exit_code=-15,
                    interrupted=True,
                    error_summary="task interrupted during shutdown",
                )
            if dataset == "explode":
                raise RuntimeError("runner exploded")
            if dataset == "fail":
                return RunnerResult(exit_code=7, error_summary="fake failure")
            return RunnerResult(exit_code=0, success=True)
        finally:
            with self.lock:
                self.active -= 1

    def terminate_all(self) -> None:
        self.terminated.set()


@pytest.mark.parametrize("max_concurrency", [-1, 0, 2, 3])
def test_scheduler_only_accepts_single_concurrency(
    tmp_path: Path, max_concurrency: int
) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])

    with pytest.raises(ValueError, match="exactly 1"):
        Scheduler(database, ControlledRunner(), max_concurrency=max_concurrency)


def test_scheduler_runs_fifo_updates_stages_and_isolates_failures(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one", "fail", "explode", "four"])
    runner = ControlledRunner(database)
    scheduler = Scheduler(
        database,
        runner,
        max_concurrency=1,
        now_factory=lambda: "2026-08-20T11:00:00Z",
    )

    scheduler.start()
    scheduler.enqueue(["task-one", "task-fail", "task-explode", "task-four"])
    assert scheduler.wait_until_idle(5)
    scheduler.stop()

    assert runner.order == ["one", "fail", "explode", "four"]
    assert runner.max_active == 1
    assert runner.entry_states == [
        ("one", "running", "preparing"),
        ("fail", "running", "preparing"),
        ("explode", "running", "preparing"),
        ("four", "running", "preparing"),
    ]
    assert runner.stage_states == [
        ("one", "running", "camera_calibration"),
        ("fail", "running", "camera_calibration"),
        ("explode", "running", "camera_calibration"),
        ("four", "running", "camera_calibration"),
    ]
    assert database.get_task("task-one")["status"] == "succeeded"  # type: ignore[index]
    failed = database.get_task("task-fail")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 7
    assert failed["error_summary"] == "fake failure"
    exploded = database.get_task("task-explode")
    assert exploded is not None
    assert exploded["status"] == "failed"
    assert "runner exploded" in exploded["error_summary"]
    assert database.get_task("task-four")["status"] == "succeeded"  # type: ignore[index]


def test_enqueue_validates_the_entire_batch_before_publishing(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    runner = ControlledRunner()
    scheduler = Scheduler(database, runner, max_concurrency=1)
    scheduler.start()

    with pytest.raises(KeyError):
        scheduler.enqueue(["task-one", "missing"])
    time.sleep(0.05)
    scheduler.stop()

    assert runner.order == []
    assert database.get_task("task-one")["status"] == "queued"  # type: ignore[index]


def test_scheduler_shutdown_interrupts_running_and_queued_and_reports_runtime(
    tmp_path: Path,
) -> None:
    database = create_database_with_tasks(tmp_path, ["slow", "two"])
    runner = ControlledRunner(database)
    scheduler = Scheduler(database, runner, max_concurrency=1)
    scheduler.start()
    scheduler.enqueue(["task-slow", "task-two"])
    assert runner.started.wait(3)

    assert scheduler.runtime_summary() == {
        "max_concurrency": 1,
        "running": 1,
        "queued": 1,
        "accepting_tasks": True,
    }
    assert scheduler.wait_until_idle(0.01) is False

    scheduler.stop()

    assert scheduler.wait_until_idle(0.1) is True
    assert scheduler.runtime_summary() == {
        "max_concurrency": 1,
        "running": 0,
        "queued": 0,
        "accepting_tasks": False,
    }
    assert database.get_task("task-slow")["status"] == "interrupted"  # type: ignore[index]
    assert database.get_task("task-two")["status"] == "interrupted"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="not accepting"):
        scheduler.enqueue(["task-two"])


def test_enqueue_racing_with_stop_cannot_publish_after_shutdown(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    scheduler = Scheduler(database, ControlledRunner(), max_concurrency=1)
    scheduler.start()
    original_get_task = database.get_task
    validation_started = threading.Event()
    release_validation = threading.Event()

    def blocked_get_task(task_id: str) -> dict[str, Any] | None:
        validation_started.set()
        assert release_validation.wait(3)
        return original_get_task(task_id)

    database.get_task = blocked_get_task  # type: ignore[method-assign]
    errors: list[Exception] = []
    enqueue_thread = threading.Thread(
        target=lambda: _capture_error(errors, lambda: scheduler.enqueue(["task-one"]))
    )
    enqueue_thread.start()
    assert validation_started.wait(3)
    stop_thread = threading.Thread(target=scheduler.stop)
    stop_thread.start()
    stop_thread.join(3)
    assert not stop_thread.is_alive()

    release_validation.set()
    enqueue_thread.join(3)

    assert errors and isinstance(errors[0], RuntimeError)
    assert database.get_task("task-one")["status"] == "interrupted"  # type: ignore[index]
    assert scheduler.wait_until_idle(0.1)


def _capture_error(errors: list[Exception], action: Callable[[], None]) -> None:
    try:
        action()
    except Exception as exc:
        errors.append(exc)


def test_start_and_stop_are_serialized_around_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    scheduler = Scheduler(database, ControlledRunner(), max_concurrency=1)
    original_start = threading.Thread.start
    worker_start_entered = threading.Event()
    release_worker_start = threading.Event()

    def blocked_worker_start(thread: threading.Thread) -> None:
        if thread.name == "ego-task-worker-1":
            worker_start_entered.set()
            assert release_worker_start.wait(3)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", blocked_worker_start)
    errors: list[Exception] = []
    start_thread = threading.Thread(
        target=lambda: _capture_error(errors, scheduler.start), name="test-start"
    )
    stop_attempted = threading.Event()
    stop_thread = threading.Thread(
        target=lambda: (
            stop_attempted.set(),
            _capture_error(errors, scheduler.stop),
        ),
        name="test-stop",
    )

    start_thread.start()
    assert worker_start_entered.wait(3)
    stop_thread.start()
    assert stop_attempted.wait(3)
    time.sleep(0.05)
    release_worker_start.set()
    start_thread.join(3)
    stop_thread.join(3)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert errors == []
    assert scheduler.wait_until_idle(0.1)
    assert scheduler._queue.unfinished_tasks == 0


def test_concurrent_stop_has_one_owner_and_no_leftover_sentinel(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    runner = ControlledRunner()
    scheduler = Scheduler(database, runner, max_concurrency=1)
    scheduler.start()
    original_terminate_all = runner.terminate_all
    terminate_entered = threading.Event()
    release_terminate = threading.Event()
    terminate_lock = threading.Lock()
    terminate_calls = 0

    def blocked_terminate_all() -> None:
        nonlocal terminate_calls
        with terminate_lock:
            terminate_calls += 1
        terminate_entered.set()
        assert release_terminate.wait(3)
        original_terminate_all()

    runner.terminate_all = blocked_terminate_all  # type: ignore[method-assign]
    errors: list[Exception] = []
    first = threading.Thread(target=lambda: _capture_error(errors, scheduler.stop))
    second_attempted = threading.Event()
    second = threading.Thread(
        target=lambda: (
            second_attempted.set(),
            _capture_error(errors, scheduler.stop),
        )
    )
    first.start()
    assert terminate_entered.wait(3)
    second.start()
    assert second_attempted.wait(3)
    time.sleep(0.05)

    with terminate_lock:
        calls_while_blocked = terminate_calls
    release_terminate.set()
    first.join(3)
    second.join(3)

    assert calls_while_blocked == 1
    assert errors == []
    assert terminate_calls == 1
    assert scheduler._queue.unfinished_tasks == 0
    scheduler.stop()
    assert scheduler._queue.unfinished_tasks == 0


def test_scheduler_stop_reports_bounded_worker_join_failure(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    release = threading.Event()
    started = threading.Event()

    class UnstoppableRunner:
        def run(
            self, task: dict[str, Any], on_stage: Callable[[str], None]
        ) -> RunnerResult:
            started.set()
            assert release.wait(3)
            return RunnerResult(exit_code=-15, interrupted=True)

        def terminate_all(self) -> None:
            pass

    scheduler = Scheduler(
        database,
        UnstoppableRunner(),
        max_concurrency=1,
        worker_join_timeout=0.05,
    )
    scheduler.start()
    scheduler.enqueue(["task-one"])
    assert started.wait(3)

    before = time.monotonic()
    with pytest.raises(RuntimeError, match="worker did not stop"):
        scheduler.stop()
    elapsed = time.monotonic() - before
    release.set()
    assert scheduler.wait_until_idle(3)

    assert elapsed < 1
    with pytest.raises(RuntimeError, match="worker did not stop"):
        scheduler.stop()
    assert scheduler._queue.unfinished_tasks == 0


def test_scheduler_is_one_shot_after_stop(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["one"])
    scheduler = Scheduler(database, ControlledRunner(), max_concurrency=1)
    scheduler.start()
    scheduler.stop()

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        scheduler.start()


def test_scheduler_recovery_marks_old_unfinished_tasks_interrupted(tmp_path: Path) -> None:
    database = create_database_with_tasks(tmp_path, ["queued", "running", "done"])
    database.transition_task("task-running", "running")
    database.transition_task("task-done", "running")
    database.transition_task("task-done", "succeeded")
    scheduler = Scheduler(
        database,
        ControlledRunner(),
        max_concurrency=1,
        now_factory=lambda: "2026-08-20T11:00:00Z",
    )

    changed = scheduler.recover_unfinished()

    assert changed == 2
    assert database.get_task("task-queued")["status"] == "interrupted"  # type: ignore[index]
    assert database.get_task("task-running")["status"] == "interrupted"  # type: ignore[index]
    assert database.get_task("task-done")["status"] == "succeeded"  # type: ignore[index]
