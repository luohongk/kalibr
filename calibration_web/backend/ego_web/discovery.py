from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from .models import DatasetInfo

REQUIRED_BAGS = (
    "calibration_4cam.bag",
    "calibration_cam0_imu.bag",
    "imu.bag",
)
RESULT_FILES = (
    "imu.yaml",
    "kalibr_input-camchain.yaml",
    "kalibr_input-camchain-imucam.yaml",
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_direct_child(root: Path, name: str) -> Path:
    relative = Path(name)
    if (
        not name
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.as_posix() != name
        or name in {".", ".."}
    ):
        raise ValueError("path must use a canonical direct-child name")
    resolved_root = root.resolve()
    candidate = resolved_root / relative
    if candidate.is_symlink():
        raise ValueError("symbolic-link identifiers are not allowed")
    resolved = candidate.resolve()
    if not _is_within(resolved, resolved_root) or resolved.parent != resolved_root:
        raise ValueError("path escapes root")
    return resolved


def _is_regular_non_symlink_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _is_valid_dataset(dataset: Path) -> bool:
    return all(_is_regular_non_symlink_file(dataset / name) for name in REQUIRED_BAGS)


def _completion_state(dataset: Path) -> bool:
    result = dataset / "result"
    if result.is_symlink():
        return True
    done = result / ".done"
    return _is_regular_non_symlink_file(done)


def discover_datasets(
    data_root: Path,
    active_tasks: Mapping[str, str] | Collection[str] | None = None,
) -> list[DatasetInfo]:
    resolved_root = data_root.resolve()
    if not resolved_root.is_dir():
        return []

    datasets: list[DatasetInfo] = []
    for dataset in sorted(resolved_root.iterdir(), key=lambda item: item.name):
        if not dataset.is_dir() or dataset.is_symlink() or not _is_valid_dataset(dataset):
            continue

        active_task_id: str | None = None
        is_busy = False
        if active_tasks is not None:
            is_busy = dataset.name in active_tasks
            if isinstance(active_tasks, Mapping) and is_busy:
                active_task_id = active_tasks[dataset.name]

        state = "completed" if _completion_state(dataset) else "busy" if is_busy else "ready"
        datasets.append(
            DatasetInfo(
                name=dataset.name,
                state=state,
                active_task_id=active_task_id,
            )
        )
    return datasets


def resolve_dataset(data_root: Path, dataset_name: str) -> Path:
    dataset = _resolve_direct_child(data_root, dataset_name)
    if not dataset.is_dir() or dataset.is_symlink() or not _is_valid_dataset(dataset):
        raise ValueError("unknown or invalid dataset")
    return dataset
