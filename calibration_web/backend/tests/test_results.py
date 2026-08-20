from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ego_web.results import ResultError, list_artifacts, read_summary, resolve_artifact


def make_result_task(
    tmp_path: Path,
    make_dataset: Callable[[Path, str], Path],
) -> tuple[Path, dict[str, object]]:
    data_root = tmp_path / "data"
    dataset = make_dataset(data_root, "dataset-a")
    result = dataset / "result"
    result.mkdir()
    (result / ".done").write_text("done")
    (result / "summary.txt").write_text("总判定: [WARN]\n")
    (result / "imu.yaml").write_text("imu: ok\n")
    (result / "report.pdf").write_bytes(b"pdf")
    task_root = tmp_path / "runtime" / "tasks" / "task-a"
    task_root.mkdir(parents=True)
    (task_root / "runner.log").write_text("runner")
    return data_root, {"dataset": "dataset-a", "task_root": str(task_root)}


def test_summary_and_artifact_listing(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    data_root, task = make_result_task(tmp_path, make_dataset)
    assert read_summary(task, data_root) == {"verdict": "WARN", "text": "总判定: [WARN]\n"}
    artifacts = list_artifacts(task, data_root)
    assert [item["path"] for item in artifacts] == [
        "result/summary.txt", "result/imu.yaml", "runner.log", "result/report.pdf"
    ]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "result/.done", "result/a/b"])
def test_artifact_resolution_rejects_unsafe_paths(
    tmp_path: Path,
    make_dataset: Callable[[Path, str], Path],
    path: str,
) -> None:
    data_root, task = make_result_task(tmp_path, make_dataset)
    with pytest.raises(ResultError):
        resolve_artifact(task, data_root, path)


def test_artifact_resolution_rejects_symlink(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    data_root, task = make_result_task(tmp_path, make_dataset)
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (data_root / "dataset-a" / "result" / "link.txt").symlink_to(outside)
    with pytest.raises(ResultError, match="symbolic"):
        resolve_artifact(task, data_root, "result/link.txt")


def test_missing_summary_is_classified(
    tmp_path: Path, make_dataset: Callable[[Path, str], Path]
) -> None:
    data_root, task = make_result_task(tmp_path, make_dataset)
    (data_root / "dataset-a" / "result" / "summary.txt").unlink()
    with pytest.raises(ResultError) as error:
        read_summary(task, data_root)
    assert error.value.code == "summary_missing"
