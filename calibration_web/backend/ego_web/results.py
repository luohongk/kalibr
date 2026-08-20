from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .discovery import resolve_dataset


class ResultError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_artifact") -> None:
        super().__init__(message)
        self.code = code


def _regular_file(path: Path) -> Path:
    if path.is_symlink():
        raise ResultError("artifact symbolic links are not allowed")
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as exc:
        raise ResultError("artifact is missing") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ResultError("artifact must be a regular file")
    return resolved


def _task_root(task: dict[str, Any]) -> Path:
    root = Path(str(task["task_root"]))
    if root.is_symlink():
        raise ResultError("task root cannot be a symbolic link")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ResultError("task root is missing") from exc


def _result_root(task: dict[str, Any], data_root: Path) -> Path:
    try:
        dataset = resolve_dataset(data_root, str(task["dataset"]))
    except (OSError, ValueError) as exc:
        raise ResultError("dataset is missing or invalid") from exc
    result = dataset / "result"
    if result.is_symlink() or not result.is_dir():
        raise ResultError("result directory is missing or unsafe")
    return result.resolve()


def resolve_artifact(task: dict[str, Any], data_root: Path, artifact_path: str) -> Path:
    relative = PurePosixPath(artifact_path)
    if (
        not artifact_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != artifact_path
    ):
        raise ResultError("artifact path is not allowed")

    if relative.parts == ("runner.log",):
        root = _task_root(task)
        candidate = _regular_file(root / "runner.log")
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ResultError("artifact is outside task root") from exc
        return candidate

    if len(relative.parts) != 2 or relative.parts[0] != "result":
        raise ResultError("artifact path is not allowed")
    filename = relative.parts[1]
    if filename in {"", ".", "..", ".done"}:
        raise ResultError("artifact path is not allowed")
    root = _result_root(task, data_root)
    candidate = _regular_file(root / filename)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResultError("artifact is outside result directory") from exc
    return candidate


def list_artifacts(task: dict[str, Any], data_root: Path) -> list[dict[str, Any]]:
    paths: list[str] = ["runner.log"]
    try:
        result_root = _result_root(task, data_root)
        paths.extend(
            f"result/{candidate.name}"
            for candidate in sorted(result_root.iterdir(), key=lambda item: item.name)
            if candidate.name != ".done"
        )
    except (OSError, ResultError):
        pass

    artifacts: list[dict[str, Any]] = []
    for relative in paths:
        try:
            resolved = resolve_artifact(task, data_root, relative)
            size = resolved.stat().st_size
        except (OSError, ResultError):
            continue
        artifacts.append({"path": relative, "name": resolved.name, "size": size})

    priority = {
        "result/summary.txt": 0,
        "result/kalibr_input-camchain-imucam.yaml": 1,
        "result/kalibr_input-camchain.yaml": 2,
        "result/imu.yaml": 3,
        "result/kalibr_input-report-imucam.pdf": 4,
        "result/kalibr_input-report-cam.pdf": 5,
        "runner.log": 6,
    }
    return sorted(
        artifacts,
        key=lambda item: (priority.get(str(item["path"]), 10), str(item["path"])),
    )


def read_summary(task: dict[str, Any], data_root: Path) -> dict[str, str | None]:
    try:
        path = resolve_artifact(task, data_root, "result/summary.txt")
    except ResultError as exc:
        raise ResultError("summary file is missing", code="summary_missing") from exc
    try:
        if path.stat().st_size > 256 * 1024:
            raise ResultError("summary file is too large", code="summary_invalid")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ResultError("summary file cannot be read", code="summary_invalid") from exc
    if not text.strip():
        raise ResultError("summary file is empty", code="summary_invalid")
    match = re.search(r"总判定:\s*\[(OK|WARN|FAIL)\]", text)
    return {"verdict": match.group(1) if match else None, "text": text}
