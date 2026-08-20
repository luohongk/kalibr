from collections.abc import Callable
from pathlib import Path

import pytest

from ego_web.discovery import REQUIRED_BAGS, RESULT_FILES, discover_datasets, resolve_dataset


MakeDataset = Callable[[Path, str], Path]


def test_kalibr_file_constants_are_stable() -> None:
    assert REQUIRED_BAGS == (
        "calibration_4cam.bag",
        "calibration_cam0_imu.bag",
        "imu.bag",
    )
    assert RESULT_FILES == (
        "imu.yaml",
        "kalibr_input-camchain.yaml",
        "kalibr_input-camchain-imucam.yaml",
    )


def test_discovery_returns_sorted_direct_complete_datasets(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    make_dataset(tmp_path, "dataset-b")
    make_dataset(tmp_path, "dataset-a")
    incomplete = make_dataset(tmp_path, "incomplete")
    (incomplete / REQUIRED_BAGS[0]).unlink()
    (tmp_path / "ordinary-file").write_text("ignore")
    make_dataset(tmp_path / "wrapper", "nested")

    datasets = discover_datasets(tmp_path)

    assert [item.model_dump() for item in datasets] == [
        {"name": "dataset-a", "state": "ready", "active_task_id": None},
        {"name": "dataset-b", "state": "ready", "active_task_id": None},
    ]


def test_discovery_requires_three_non_symlink_regular_bag_files(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    missing = make_dataset(tmp_path, "missing")
    (missing / REQUIRED_BAGS[0]).unlink()

    directory_bag = make_dataset(tmp_path, "directory-bag")
    (directory_bag / REQUIRED_BAGS[1]).unlink()
    (directory_bag / REQUIRED_BAGS[1]).mkdir()

    symlink_bag = make_dataset(tmp_path, "symlink-bag")
    outside_bag = tmp_path / "outside.bag"
    outside_bag.write_bytes(b"bag")
    (symlink_bag / REQUIRED_BAGS[2]).unlink()
    (symlink_bag / REQUIRED_BAGS[2]).symlink_to(outside_bag)

    assert discover_datasets(tmp_path) == []


def test_discovery_ignores_dataset_directory_symlinks(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = make_dataset(tmp_path / "outside", "dataset")
    (data_root / "linked-dataset").symlink_to(outside, target_is_directory=True)

    assert discover_datasets(data_root) == []


def test_discovery_reports_ready_busy_and_completed_states(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    make_dataset(tmp_path, "ready")
    make_dataset(tmp_path, "busy-with-id")
    make_dataset(tmp_path, "busy-without-id")
    completed = make_dataset(tmp_path, "completed")
    (completed / "result").mkdir()
    (completed / "result" / ".done").touch()

    datasets = discover_datasets(
        tmp_path,
        {"busy-with-id": "task-123", "completed": "task-finished"},
    )
    by_name = {item.name: item for item in datasets}

    assert by_name["ready"].state == "ready"
    assert by_name["ready"].active_task_id is None
    assert by_name["busy-with-id"].state == "busy"
    assert by_name["busy-with-id"].active_task_id == "task-123"
    assert by_name["completed"].state == "completed"
    assert by_name["completed"].active_task_id == "task-finished"

    collection_result = discover_datasets(tmp_path, {"busy-without-id"})
    collection_by_name = {item.name: item for item in collection_result}
    assert collection_by_name["busy-without-id"].state == "busy"
    assert collection_by_name["busy-without-id"].active_task_id is None


def test_done_must_be_a_non_symlink_regular_file(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    directory_done = make_dataset(tmp_path, "directory-done")
    (directory_done / "result" / ".done").mkdir(parents=True)

    symlink_done = make_dataset(tmp_path, "symlink-done")
    (symlink_done / "result").mkdir()
    outside_done = tmp_path / "outside.done"
    outside_done.touch()
    (symlink_done / "result" / ".done").symlink_to(outside_done)

    states = {item.name: item.state for item in discover_datasets(tmp_path)}

    assert states == {"directory-done": "ready", "symlink-done": "ready"}


def test_result_directory_symlink_fails_closed(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    dataset = make_dataset(tmp_path, "unsafe-result")
    outside_result = tmp_path / "outside-result"
    outside_result.mkdir()
    (dataset / "result").symlink_to(outside_result, target_is_directory=True)

    discovered = discover_datasets(tmp_path)

    assert discovered[0].name == "unsafe-result"
    assert discovered[0].state == "completed"


@pytest.mark.parametrize(
    "identifier",
    ["", ".", "..", "../dataset", "./dataset", "nested/dataset", "/tmp/dataset"],
)
def test_dataset_resolution_rejects_non_direct_child_names(
    tmp_path: Path, identifier: str
) -> None:
    with pytest.raises(ValueError):
        resolve_dataset(tmp_path, identifier)


def test_dataset_resolution_rejects_symlinks_and_invalid_bags(
    tmp_path: Path, make_dataset: MakeDataset
) -> None:
    valid = make_dataset(tmp_path, "valid")

    linked = tmp_path / "linked"
    linked.symlink_to(valid, target_is_directory=True)

    missing = make_dataset(tmp_path, "missing")
    (missing / REQUIRED_BAGS[0]).unlink()

    directory_bag = make_dataset(tmp_path, "directory-bag")
    (directory_bag / REQUIRED_BAGS[1]).unlink()
    (directory_bag / REQUIRED_BAGS[1]).mkdir()

    symlink_bag = make_dataset(tmp_path, "symlink-bag")
    (symlink_bag / REQUIRED_BAGS[2]).unlink()
    (symlink_bag / REQUIRED_BAGS[2]).symlink_to(valid / REQUIRED_BAGS[2])

    for name in ("linked", "missing", "directory-bag", "symlink-bag", "unknown"):
        with pytest.raises(ValueError):
            resolve_dataset(tmp_path, name)

    assert resolve_dataset(tmp_path, "valid") == valid.resolve()
