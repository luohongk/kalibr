from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import pytest

from ego_web.db import Database, InvalidStatusTransition


def group_record(group_id: str = "group-1", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": group_id,
        "created_at": "2026-08-06T08:00:00Z",
    }
    record.update(overrides)
    return record


def task_record(
    task_id: str,
    dataset: str,
    group_id: str = "group-1",
    **overrides: object,
) -> dict[str, object]:
    record: dict[str, object] = {
        "id": task_id,
        "group_id": group_id,
        "dataset": dataset,
        "status": "queued",
        "stage": None,
        "created_at": "2026-08-06T08:00:00Z",
        "task_root": f"/runtime/{task_id}",
    }
    record.update(overrides)
    return record


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "kalibr.sqlite3")
    database.initialize()
    return database


class CoordinatedCursor:
    def __init__(self, cursor: sqlite3.Cursor, barrier: Barrier) -> None:
        self.cursor = cursor
        self.barrier = barrier

    def fetchone(self) -> sqlite3.Row | None:
        row = self.cursor.fetchone()
        self.barrier.wait(timeout=2)
        return row


class CoordinatedDeferredConnection:
    def __init__(self, connection: sqlite3.Connection, barrier: Barrier) -> None:
        self.connection = connection
        self.barrier = barrier
        self.has_updated_task = False

    def __enter__(self) -> CoordinatedDeferredConnection:
        self.barrier.wait(timeout=2)
        self.connection.execute("BEGIN")
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.__exit__(*args)

    def execute(self, sql: str, parameters: object = ()) -> Any:
        normalized_sql = " ".join(sql.lower().split())
        if normalized_sql.startswith("update tasks set"):
            self.has_updated_task = True
        cursor = self.connection.execute(sql, parameters)
        if (
            not self.has_updated_task
            and normalized_sql == "select status from tasks where id = ?"
        ):
            return CoordinatedCursor(cursor, self.barrier)
        return cursor


class SignalingConnection:
    def __init__(self, connection: sqlite3.Connection, update_started: Event) -> None:
        self.connection = connection
        self.update_started = update_started

    def __enter__(self) -> SignalingConnection:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.__exit__(*args)

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if "set stage = ?" in " ".join(sql.lower().split()):
            self.update_started.set()
        return self.connection.execute(sql, parameters)


def test_initialize_creates_exact_schema_and_indexes(db: Database) -> None:
    with db.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

        group_columns = {
            row[1]: (row[2], row[3], row[5])
            for row in connection.execute("PRAGMA table_info(task_groups)")
        }
        task_columns = {
            row[1]: (row[2], row[3], row[5])
            for row in connection.execute("PRAGMA table_info(tasks)")
        }
        indexes = {
            row[1]: row[4]
            for row in connection.execute("PRAGMA index_list(tasks)")
        }
        index_sql = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'"
            )
            if row[1] is not None
        }
        foreign_keys = list(connection.execute("PRAGMA foreign_key_list(tasks)"))
        tasks_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()[0]

    assert set(group_columns) == {"id", "created_at"}
    assert group_columns["id"] == ("TEXT", 0, 1)
    assert group_columns["created_at"] == ("TEXT", 1, 0)
    assert set(task_columns) == {
        "id",
        "group_id",
        "dataset",
        "status",
        "stage",
        "created_at",
        "started_at",
        "finished_at",
        "task_root",
        "exit_code",
        "error_summary",
    }
    assert task_columns["id"] == ("TEXT", 0, 1)
    assert task_columns["group_id"][1] == 1
    assert task_columns["dataset"] == ("TEXT", 1, 0)
    assert task_columns["stage"] == ("TEXT", 0, 0)
    assert task_columns["task_root"][1] == 1
    assert "idx_tasks_status_created" in indexes
    assert "idx_tasks_group_id" in indexes
    assert "idx_tasks_dataset" in indexes
    assert indexes["idx_tasks_active_dataset"] == 1
    normalized_active_sql = " ".join(index_sql["idx_tasks_active_dataset"].lower().split())
    assert "on tasks(dataset)" in normalized_active_sql
    assert "where status in ('queued', 'running')" in normalized_active_sql
    assert any(
        row[2] == "task_groups" and row[3] == "group_id" and row[6].upper() == "CASCADE"
        for row in foreign_keys
    )
    normalized_tasks_sql = " ".join(tasks_sql.lower().split())
    for status in ("queued", "running", "succeeded", "failed", "interrupted"):
        assert f"'{status}'" in normalized_tasks_sql
    assert "extracting" not in normalized_tasks_sql
    assert "vio" not in normalized_tasks_sql


def test_initialize_rejects_newer_or_unknown_existing_schema(tmp_path: Path) -> None:
    newer_path = tmp_path / "newer.sqlite3"
    with sqlite3.connect(newer_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(RuntimeError, match="database schema version"):
        Database(newer_path).initialize()

    unknown_path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown_path) as connection:
        connection.execute("CREATE TABLE legacy (id INTEGER)")
    with pytest.raises(RuntimeError, match="unknown unversioned database"):
        Database(unknown_path).initialize()


def test_create_task_group_inserts_group_and_tasks_atomically(db: Database) -> None:
    db.create_task_group(
        group_record(),
        [task_record("task-2", "dataset-b"), task_record("task-1", "dataset-a")],
    )

    group = db.get_task_group("group-1")
    assert group == {
        "id": "group-1",
        "created_at": "2026-08-06T08:00:00Z",
        "task_count": 2,
        "aggregate_status": "queued",
    }
    assert [task["id"] for task in db.list_tasks(group_id="group-1")] == [
        "task-1",
        "task-2",
    ]
    details = db.get_task_details("task-1")
    assert details is not None
    assert set(details) == {
        "id",
        "group_id",
        "dataset",
        "status",
        "stage",
        "created_at",
        "started_at",
        "finished_at",
        "task_root",
        "exit_code",
        "error_summary",
        "group_created_at",
    }
    assert details["group_created_at"] == "2026-08-06T08:00:00Z"


def test_create_task_group_rejects_empty_or_cross_group_tasks(db: Database) -> None:
    with pytest.raises(ValueError, match="at least one task"):
        db.create_task_group(group_record(), [])
    with pytest.raises(ValueError, match="same group"):
        db.create_task_group(
            group_record(),
            [task_record("task-1", "dataset-a", group_id="another-group")],
        )

    assert db.get_task_group("group-1") is None
    assert db.list_tasks() == []


def test_create_task_group_rolls_back_group_and_tasks_on_insert_failure(db: Database) -> None:
    tasks = [
        task_record("task-1", "dataset-a", task_root="/runtime/shared"),
        task_record("task-2", "dataset-b", task_root="/runtime/shared"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        db.create_task_group(group_record(), tasks)

    assert db.get_task_group("group-1") is None
    assert db.list_tasks() == []


def test_active_dataset_is_unique_but_terminal_task_allows_new_task(db: Database) -> None:
    db.create_task_group(group_record(), [task_record("task-1", "dataset-a")])

    with pytest.raises(sqlite3.IntegrityError):
        db.create_task_group(
            group_record("group-2"),
            [task_record("task-2", "dataset-a", "group-2")],
        )
    assert db.get_task_group("group-2") is None

    db.transition_task("task-1", "failed", finished_at="2026-08-06T08:01:00Z")
    db.create_task_group(
        group_record("group-3"),
        [task_record("task-3", "dataset-a", "group-3")],
    )

    assert db.get_task("task-3")["status"] == "queued"  # type: ignore[index]


def test_status_transitions_and_stage_updates_are_separate(db: Database) -> None:
    db.create_task_group(group_record(), [task_record("task-1", "dataset-a")])

    db.update_task_stage("task-1", "extracting")
    task = db.get_task("task-1")
    assert task is not None
    assert task["status"] == "queued"
    assert task["stage"] == "extracting"

    db.transition_task("task-1", "running", started_at="2026-08-06T08:01:00Z")
    db.update_task_stage("task-1", "calibrating")
    db.transition_task(
        "task-1",
        "succeeded",
        finished_at="2026-08-06T08:09:00Z",
        exit_code=0,
        error_summary=None,
    )

    task = db.get_task("task-1")
    assert task is not None
    assert task["status"] == "succeeded"
    assert task["stage"] == "calibrating"
    assert task["started_at"] == "2026-08-06T08:01:00Z"
    assert task["finished_at"] == "2026-08-06T08:09:00Z"
    assert task["exit_code"] == 0
    with pytest.raises(InvalidStatusTransition, match="terminal"):
        db.update_task_stage("task-1", "publishing")
    assert db.get_task("task-1")["status"] == "succeeded"  # type: ignore[index]


def test_concurrent_transitions_return_one_clear_conflict(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.create_task_group(group_record(), [task_record("task-1", "dataset-a")])
    both_read_queued = Barrier(2)
    original_connect = db.connect

    @contextmanager
    def coordinated_connect() -> Any:
        with original_connect() as connection:
            yield CoordinatedDeferredConnection(connection, both_read_queued)

    monkeypatch.setattr(db, "connect", coordinated_connect)

    def transition() -> Exception | None:
        try:
            db.transition_task("task-1", "running")
        except Exception as error:
            return error
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transition) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]

    assert sum(result is None for result in results) == 1
    conflicts = [result for result in results if result is not None]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], InvalidStatusTransition)


def test_stage_update_reports_terminal_conflict_after_waiting_for_writer(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.create_task_group(group_record(), [task_record("task-1", "dataset-a")])
    update_started = Event()
    original_connect = db.connect

    @contextmanager
    def signaling_connect() -> Any:
        with original_connect() as connection:
            yield SignalingConnection(connection, update_started)

    monkeypatch.setattr(db, "connect", signaling_connect)

    with original_connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE tasks SET status = 'interrupted' WHERE id = 'task-1'")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(db.update_task_stage, "task-1", "extracting")
            assert update_started.wait(timeout=2)
            writer.commit()
            with pytest.raises(InvalidStatusTransition, match="terminal"):
                future.result(timeout=5)


def test_illegal_status_transitions_and_fields_are_rejected(db: Database) -> None:
    db.create_task_group(group_record(), [task_record("task-1", "dataset-a")])

    with pytest.raises(InvalidStatusTransition):
        db.transition_task("task-1", "succeeded")
    with pytest.raises(InvalidStatusTransition, match="unknown task status"):
        db.transition_task("task-1", "extracting")
    with pytest.raises(ValueError, match="runner_status"):
        db.transition_task("task-1", "running", runner_status="RUNNING")
    with pytest.raises(KeyError):
        db.transition_task("missing", "running")
    with pytest.raises(KeyError):
        db.update_task_stage("missing", "extracting")

    db.transition_task("task-1", "interrupted")
    with pytest.raises(InvalidStatusTransition):
        db.transition_task("task-1", "running")


def test_list_tasks_combines_filters_and_uses_stable_pagination(db: Database) -> None:
    db.create_task_group(
        group_record("group-1"),
        [
            task_record("task-1", "dataset-a", created_at="2026-08-06T08:00:00Z"),
            task_record("task-2", "dataset-b", created_at="2026-08-06T08:01:00Z"),
        ],
    )
    db.create_task_group(
        group_record("group-2", created_at="2026-08-06T08:02:00Z"),
        [
            task_record(
                "task-3",
                "dataset-a",
                "group-2",
                status="failed",
                created_at="2026-08-06T08:02:00Z",
            )
        ],
    )
    db.transition_task("task-2", "running")

    assert [task["id"] for task in db.list_tasks(status="queued")] == ["task-1"]
    assert [task["id"] for task in db.list_tasks(dataset="dataset-a")] == [
        "task-3",
        "task-1",
    ]
    assert [task["id"] for task in db.list_tasks(group_id="group-1")] == [
        "task-2",
        "task-1",
    ]
    assert [task["id"] for task in db.list_tasks(limit=1, offset=1)] == ["task-2"]
    with pytest.raises(ValueError, match="pagination"):
        db.list_tasks(limit=0)


def test_group_aggregation_and_group_listing(db: Database) -> None:
    db.create_task_group(
        group_record("queued-group", created_at="2026-08-06T08:00:00Z"),
        [task_record("task-q", "dataset-q", "queued-group")],
    )
    db.create_task_group(
        group_record("success-group", created_at="2026-08-06T08:01:00Z"),
        [task_record("task-s", "dataset-s", "success-group")],
    )
    db.transition_task("task-s", "running")
    db.transition_task("task-s", "succeeded")
    db.create_task_group(
        group_record("running-group", created_at="2026-08-06T08:02:00Z"),
        [
            task_record("task-r", "dataset-r", "running-group"),
            task_record("task-f", "dataset-f", "running-group", status="failed"),
        ],
    )
    db.create_task_group(
        group_record("error-group", created_at="2026-08-06T08:03:00Z"),
        [
            task_record("task-e1", "dataset-e1", "error-group", status="failed"),
            task_record("task-e2", "dataset-e2", "error-group", status="interrupted"),
        ],
    )

    assert db.get_task_group("queued-group")["aggregate_status"] == "queued"  # type: ignore[index]
    assert db.get_task_group("success-group")["aggregate_status"] == "succeeded"  # type: ignore[index]
    assert db.get_task_group("running-group")["aggregate_status"] == "running"  # type: ignore[index]
    assert db.get_task_group("error-group")["aggregate_status"] == "completed_with_errors"  # type: ignore[index]
    assert [group["id"] for group in db.list_task_groups(limit=2, offset=1)] == [
        "running-group",
        "success-group",
    ]


def test_mark_unfinished_interrupted_preserves_terminal_tasks(db: Database) -> None:
    db.create_task_group(
        group_record(),
        [
            task_record("task-queued", "dataset-q"),
            task_record("task-running", "dataset-r"),
            task_record("task-failed", "dataset-f", status="failed"),
        ],
    )
    db.transition_task("task-running", "running")

    changed = db.mark_unfinished_interrupted("2026-08-06T09:00:00Z")

    assert changed == 2
    assert db.get_task("task-queued")["status"] == "interrupted"  # type: ignore[index]
    assert db.get_task("task-running")["status"] == "interrupted"  # type: ignore[index]
    assert db.get_task("task-running")["finished_at"] == "2026-08-06T09:00:00Z"  # type: ignore[index]
    assert db.get_task("task-failed")["status"] == "failed"  # type: ignore[index]
