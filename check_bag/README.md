# check_bag

Pre-flight checks for ROS1 bag files before running Kalibr calibration.

The goal is to catch bad or insufficient data collection early, before spending
time on a full optimization run.

## What It Checks

- Requested camera and IMU topics exist and contain messages.
- Header timestamps are monotonic.
- Per-topic duration, rate, median/p95/max interval, and large gaps.
- Bag receive time versus message header time offset.
- Time overlap across all requested topics.
- Multi-camera nearest timestamp synchronization.
- Image count, rate, resolution consistency, and message type.
- IMU rate, gyro excitation, acceleration variation, and active rotation axes.
- Optional target detection count, ratio, board center coverage, corner count,
  and apparent board scale variation. Without a camera YAML, checkerboard
  targets are checked with OpenCV directly.
- Checkerboard collection diversity: image edge coverage, corner coverage over
  the frame, image-plane board rotation bins, perspective tilt directions, and
  near/far apparent scale variation.

## Basic Usage

source /opt/ros/noetic/setup.bash

python3 check_bag/check_bag.py --bag /data/T1_calib_data/calibration.bag --target /catkin_ws/src/kalibr/check_bag/config/checkerboard.yaml --autodiscover --bag-freq 15 --progress-interval 1 --target-workers 16 --report-dir /catkin_ws/src/kalibr/check_bag/output

Run inside a ROS1/Kalibr environment where `rosbag` is importable:

If you only have a bag and the checkerboard target YAML:

```bash
python3 check_bag/check_bag.py \
  --bag data.bag \
  --target check_bag/config/checkerboard.yaml \
  --autodiscover \
  --bag-freq 4
```

This automatically discovers `sensor_msgs/Image`, `sensor_msgs/CompressedImage`,
and `sensor_msgs/Imu` topics from the bag. It does not require `camchain.yaml`
or `imu.yaml`.

The tool prints progress to stderr while it runs, for example topic discovery,
bag message scanning, checkerboard detection progress, and per-topic detection
summaries. This keeps `--json` output on stdout machine-readable.

Tune or disable progress output:

```bash
python3 check_bag/check_bag.py \
  --bag data.bag \
  --target check_bag/config/checkerboard.yaml \
  --autodiscover \
  --progress-interval 1.0 \
  --target-workers 8 \
  --report-dir check_bag_report

python3 check_bag/check_bag.py \
  --bag data.bag \
  --target check_bag/config/checkerboard.yaml \
  --autodiscover \
  --quiet
```

For the collection style you described, the most important warnings are:

When `--report-dir` is set, the tool writes:

- `index.html`: visual summary report.
- `report.json`: machine-readable check summary.
- `*_centers.png`: checkerboard center coverage per image topic.
- `*_corners.png`: detected checkerboard corner coverage per image topic.
- `*_hist.png`: board apparent area and image-plane roll histograms.
- `target_edge_coverage`: the checkerboard did not reach left/right/top/bottom
  image border regions.
- `target_corner_coverage`: detected checkerboard corners do not cover enough
  of the image grid.
- `target_roll_coverage`: the board was not rotated through enough image-plane
  angles.
- `target_tilt_coverage`: the board did not show enough perspective tilt
  directions.
- `target_scale_coverage`: the board did not appear at sufficiently different
  distances or sizes.

If you already have Kalibr camera/IMU YAML files:

```bash
python3 check_bag/check_bag.py \
  --bag data.bag \
  --cams camchain.yaml \
  --imu imu.yaml
```

Enable Kalibr-backed calibration target detection when camera intrinsics are
available:

```bash
python3 check_bag/check_bag.py \
  --bag data.bag \
  --cams camchain.yaml \
  --imu imu.yaml \
  --target aprilgrid.yaml \
  --bag-freq 4
```

If you do not have YAML files ready yet but want to specify topics manually:

```bash
python3 check_bag/check_bag.py \
  --bag data.bag \
  --image-topics /cam0/image_raw /cam1/image_raw \
  --imu-topics /imu0 \
  --autodiscover
```

Machine-readable report:

```bash
python3 check_bag/check_bag.py --bag data.bag --cams camchain.yaml --imu imu.yaml --json
```

## Exit Code

- `0`: no failed checks. Warnings may still be present.
- `1`: at least one failed check.
- `2`: the tool itself could not run, for example because `rosbag` is missing.

## Tuning Thresholds

The defaults are intentionally conservative warnings, not hard proof that a
dataset will calibrate well. Common thresholds:

```bash
--min-duration 60
--min-images 80
--min-image-hz 2
--min-imu-hz 50
--max-gap-ratio 3
--min-overlap-ratio 0.8
--max-camera-sync-ms 5
--min-gyro-p95 0.25
--min-accel-norm-std 0.20
--min-target-observations 40
--min-target-detection-ratio 0.20
--min-target-center-coverage 0.35
--min-target-corner-coverage 0.45
--min-target-edge-sides 4
--min-target-roll-bins 4
--min-target-tilt-sides 2
--min-target-area-ratio 2.0
```

## Notes

Checkerboard target detection can run without camera intrinsics. Aprilgrid,
circlegrid, and charuco target checks still need `--cams` so the tool can reuse
Kalibr's camera model and compiled target detectors. If those modules are not
available, the tool still reports all base bag timing and IMU/image checks and
emits a warning explaining why target checks were skipped.
