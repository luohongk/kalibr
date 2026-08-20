from __future__ import annotations

import json
import stat as stat_module
import time
from collections.abc import Iterator
from pathlib import Path

from .db import Database, TERMINAL_STATUSES


def _sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(
        "data: "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    )
    return "\n".join(lines) + "\n\n"


def _public_task(task: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in task.items() if key != "task_root"}


def log_file_size(log_path: Path) -> int:
    try:
        stat = log_path.stat()
    except OSError:
        return 0
    return stat.st_size if stat_module.S_ISREG(stat.st_mode) else 0


def read_utf8_increment(
    log_path: Path,
    offset: int,
    *,
    final: bool = False,
) -> tuple[str, int]:
    try:
        with log_path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
    except (FileNotFoundError, IsADirectoryError):
        return "", offset
    if not data:
        return "", offset
    try:
        return data.decode("utf-8"), offset + len(data)
    except UnicodeDecodeError as exc:
        if (
            not final
            and exc.reason == "unexpected end of data"
            and exc.end == len(data)
        ):
            complete = data[: exc.start]
            return complete.decode("utf-8"), offset + len(complete)
        return data.decode("utf-8", errors="replace"), offset + len(data)


def stream_task_events(
    database: Database,
    task_id: str,
    *,
    offset: int = 0,
    poll_interval: float = 0.25,
    heartbeat_interval: float = 15.0,
) -> Iterator[str]:
    task = database.get_task_details(task_id)
    if task is None:
        raise KeyError(task_id)
    log_path = Path(task["task_root"]) / "runner.log"
    previous_status = str(task["status"])
    yield _sse("snapshot", {"task": _public_task(task), "offset": offset})
    last_emit = time.monotonic()

    while True:
        log_size = log_file_size(log_path)
        reset = log_size < offset
        if reset:
            offset = 0

        text, new_offset = read_utf8_increment(log_path, offset)
        if text or reset:
            offset = new_offset
            payload: dict[str, object] = {"offset": offset, "text": text}
            if reset:
                payload["reset"] = True
            yield _sse("log", payload, offset)
            last_emit = time.monotonic()

        current = database.get_task_details(task_id)
        if current is None:
            return
        current_status = str(current["status"])
        if current_status != previous_status:
            yield _sse("status", {"status": current_status})
            previous_status = current_status
            last_emit = time.monotonic()

        log_size = log_file_size(log_path)
        if current_status in TERMINAL_STATUSES:
            if offset < log_size:
                final_text, final_offset = read_utf8_increment(
                    log_path, offset, final=True
                )
                if final_offset > offset:
                    offset = final_offset
                    yield _sse(
                        "log",
                        {"offset": offset, "text": final_text},
                        offset,
                    )
            try:
                log_size = log_path.stat().st_size
            except (FileNotFoundError, OSError):
                log_size = 0
            if offset >= log_size:
                yield _sse(
                    "terminal",
                    {"status": current_status, "offset": offset},
                )
                return

        if time.monotonic() - last_emit >= heartbeat_interval:
            yield ": heartbeat\n\n"
            last_emit = time.monotonic()

        if poll_interval > 0:
            time.sleep(poll_interval)
