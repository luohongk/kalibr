from __future__ import annotations

import json
from pathlib import Path

from ego_web.db import Database
from ego_web.events import read_utf8_increment, stream_task_events


def make_task(tmp_path: Path, status: str = "succeeded") -> tuple[Database, Path]:
    database = Database(tmp_path / "events.sqlite3")
    database.initialize()
    task_root = tmp_path / "tasks" / "task-1"
    task_root.mkdir(parents=True)
    database.create_task_group(
        {"id": "group-1", "created_at": "2026-08-06T12:00:00Z"},
        [{
            "id": "task-1", "group_id": "group-1", "dataset": "dataset-a",
            "status": "queued", "created_at": "2026-08-06T12:00:00Z",
            "task_root": str(task_root),
        }],
    )
    if status != "queued":
        database.transition_task("task-1", "running")
    if status in {"succeeded", "failed", "interrupted"}:
        database.transition_task("task-1", status)
    return database, task_root


def event_payload(event: str) -> dict[str, object]:
    data = next(line for line in event.splitlines() if line.startswith("data: "))
    return json.loads(data[6:])


def test_terminal_stream_emits_snapshot_log_and_terminal(tmp_path: Path) -> None:
    database, task_root = make_task(tmp_path)
    (task_root / "runner.log").write_text("标定完成\n")
    events = list(stream_task_events(database, "task-1", poll_interval=0))
    assert "event: snapshot" in events[0]
    assert event_payload(events[0])["task"]["dataset"] == "dataset-a"  # type: ignore[index]
    assert any("event: log" in event and "标定完成" in event for event in events)
    assert "event: terminal" in events[-1]


def test_utf8_reader_holds_incomplete_character(tmp_path: Path) -> None:
    log = tmp_path / "runner.log"
    encoded = "标".encode()
    log.write_bytes(encoded[:-1])
    assert read_utf8_increment(log, 0) == ("", 0)
    log.write_bytes(encoded)
    assert read_utf8_increment(log, 0) == ("标", len(encoded))


def test_stream_from_offset_reads_only_new_bytes(tmp_path: Path) -> None:
    database, task_root = make_task(tmp_path, status="failed")
    log = task_root / "runner.log"
    log.write_text("old\nnew\n")
    events = list(stream_task_events(database, "task-1", offset=4, poll_interval=0))
    log_events = [event_payload(event) for event in events if "event: log" in event]
    assert log_events[0]["text"] == "new\n"
