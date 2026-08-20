import sys
from collections.abc import Callable
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1]))


REQUIRED_BAGS = (
    "calibration_4cam.bag",
    "calibration_cam0_imu.bag",
    "imu.bag",
)


@pytest.fixture
def make_dataset() -> Callable[[Path, str], Path]:
    def _make(data_root: Path, name: str) -> Path:
        dataset = data_root / name
        dataset.mkdir(parents=True, exist_ok=True)
        for bag_name in REQUIRED_BAGS:
            (dataset / bag_name).write_bytes(b"bag")
        return dataset

    return _make
