from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TASK_STATUSES = ("queued", "running", "succeeded", "failed", "interrupted")
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}
ALLOWED_TRANSITIONS = {
    "queued": {"running", "failed", "interrupted"},
    "running": {"succeeded", "failed", "interrupted"},
    "succeeded": set(),
    "failed": set(),
    "interrupted": set(),
}


class InvalidStatusTransition(ValueError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            existing_tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            if version > 1:
                raise RuntimeError(f"database schema version {version} is newer than supported version 1")
            if version == 0 and existing_tables:
                raise RuntimeError("unknown unversioned database schema")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_groups (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL REFERENCES task_groups(id) ON DELETE CASCADE,
                    dataset TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
                    ),
                    stage TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    task_root TEXT NOT NULL UNIQUE,
                    exit_code INTEGER,
                    error_summary TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_group_id
                    ON tasks(group_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_dataset
                    ON tasks(dataset);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_dataset
                    ON tasks(dataset)
                    WHERE status IN ('queued', 'running');
                PRAGMA user_version = 1;
                """
            )
            connection.commit()

    def create_task_group(
        self,
        group: Mapping[str, object],
        tasks: Iterable[Mapping[str, object]],
    ) -> None:
        task_records = list(tasks)
        if not task_records:
            raise ValueError("task group requires at least one task")
        group_id = group["id"]
        if any(task.get("group_id") != group_id for task in task_records):
            raise ValueError("all tasks must belong to the same group")

        with self.connect() as connection:
            with connection:
                connection.execute(
                    "INSERT INTO task_groups (id, created_at) VALUES (?, ?)",
                    (group_id, group["created_at"]),
                )
                for task in task_records:
                    connection.execute(
                        """
                        INSERT INTO tasks (
                            id, group_id, dataset, status, stage, created_at,
                            started_at, finished_at, task_root, exit_code, error_summary
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task["id"],
                            group_id,
                            task["dataset"],
                            task.get("status", "queued"),
                            task.get("stage"),
                            task["created_at"],
                            task.get("started_at"),
                            task.get("finished_at"),
                            task["task_root"],
                            task.get("exit_code"),
                            task.get("error_summary"),
                        ),
                    )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_task_details(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT t.*, g.created_at AS group_created_at
                FROM tasks AS t
                JOIN task_groups AS g ON g.id = t.group_id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_task_groups(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._validate_pagination(limit, offset)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_groups
                ORDER BY created_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        groups: list[dict[str, Any]] = []
        for row in rows:
            group = self.get_task_group(str(row["id"]))
            if group is not None:
                groups.append(group)
        return groups

    def get_task_group(self, group_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            group = connection.execute(
                "SELECT * FROM task_groups WHERE id = ?", (group_id,)
            ).fetchone()
            if group is None:
                return None
            statuses = [
                str(row[0])
                for row in connection.execute(
                    "SELECT status FROM tasks WHERE group_id = ?", (group_id,)
                )
            ]
        result = dict(group)
        result["task_count"] = len(statuses)
        result["aggregate_status"] = self._aggregate_status(statuses)
        return result

    @staticmethod
    def _aggregate_status(statuses: list[str]) -> str:
        if statuses and all(status == "queued" for status in statuses):
            return "queued"
        if statuses and all(status == "succeeded" for status in statuses):
            return "succeeded"
        if any(status not in TERMINAL_STATUSES for status in statuses):
            return "running"
        return "completed_with_errors"

    def transition_task(self, task_id: str, new_status: str, **fields: object) -> None:
        allowed_fields = {"started_at", "finished_at", "exit_code", "error_summary"}
        unknown_fields = fields.keys() - allowed_fields
        if unknown_fields:
            raise ValueError(f"unsupported task fields: {', '.join(sorted(unknown_fields))}")
        if new_status not in TASK_STATUSES:
            raise InvalidStatusTransition(f"unknown task status: {new_status}")

        source_statuses = [
            status for status, targets in ALLOWED_TRANSITIONS.items() if new_status in targets
        ]
        placeholders = ", ".join("?" for _ in source_statuses)
        assignments = ["status = ?"]
        values: list[object] = [new_status]
        for name, value in fields.items():
            assignments.append(f"{name} = ?")
            values.append(value)
        values.extend((task_id, *source_statuses))

        with self.connect() as connection:
            with connection:
                cursor = connection.execute(
                    f"""
                    UPDATE tasks
                    SET {', '.join(assignments)}
                    WHERE id = ? AND status IN ({placeholders})
                    """,
                    values,
                )
                if cursor.rowcount == 1:
                    return
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                raise InvalidStatusTransition(
                    f"cannot transition task {task_id} from {row['status']} to {new_status}"
                )

    def update_task_stage(self, task_id: str, stage: str | None) -> None:
        with self.connect() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET stage = ?
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (stage, task_id),
                )
                if cursor.rowcount == 1:
                    return
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                raise InvalidStatusTransition(
                    f"cannot update stage for terminal task {task_id} in status {row['status']}"
                )

    def list_tasks(
        self,
        *,
        status: str | None = None,
        dataset: str | None = None,
        group_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._validate_pagination(limit, offset)
        clauses: list[str] = []
        values: list[object] = []
        for column, value in (
            ("status", status),
            ("dataset", dataset),
            ("group_id", group_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, offset))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM tasks
                {where}
                ORDER BY created_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_unfinished_interrupted(self, finished_at: str) -> int:
        with self.connect() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'interrupted', finished_at = ?
                    WHERE status IN ('queued', 'running')
                    """,
                    (finished_at,),
                )
                return cursor.rowcount

    def delete_inactive_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE id = ? AND status IN ('queued', 'succeeded', 'failed', 'interrupted')
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    return None
                task = dict(row)
                connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                connection.execute(
                    "DELETE FROM task_groups WHERE id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM tasks WHERE group_id = ?)",
                    (task["group_id"], task["group_id"]),
                )
                return task

    @staticmethod
    def _validate_pagination(limit: int, offset: int) -> None:
        if limit < 1 or limit > 500 or offset < 0:
            raise ValueError("invalid pagination")
