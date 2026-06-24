#!/usr/bin/env python3
"""Pre-flight checks for Kalibr calibration rosbag datasets.

The tool is intentionally useful before a full Kalibr run: it checks that the
requested camera/IMU topics exist, that timing is sane, that the bag has enough
duration and motion, and optionally that the calibration target is detected with
reasonable image coverage.
"""

from __future__ import annotations

import argparse
import bisect
import html
import json
import math
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import yaml


IMAGE_TYPES = {
    "sensor_msgs/Image",
    "sensor_msgs/CompressedImage",
    "mv_cameras/ImageSnappyMsg",
}
IMU_TYPES = {"sensor_msgs/Imu"}


@dataclass
class Check:
    level: str
    name: str
    message: str


@dataclass
class TopicStats:
    topic: str
    msg_type: str = "unknown"
    count: int = 0
    first_header: Optional[float] = None
    last_header: Optional[float] = None
    first_bag: Optional[float] = None
    last_bag: Optional[float] = None
    header_dts: List[float] = field(default_factory=list)
    bag_header_offsets: List[float] = field(default_factory=list)
    non_monotonic: int = 0
    timestamps: List[float] = field(default_factory=list)
    image_shapes: Dict[str, int] = field(default_factory=dict)
    image_encodings: Dict[str, int] = field(default_factory=dict)
    gyro: List[Tuple[float, float, float]] = field(default_factory=list)
    accel: List[Tuple[float, float, float]] = field(default_factory=list)

    def add(self, msg: Any, bag_time: float) -> None:
        header_time = get_header_time(msg, default=bag_time)
        if self.count == 0:
            self.msg_type = getattr(msg, "_type", self.msg_type)
            self.first_header = header_time
            self.first_bag = bag_time
        else:
            assert self.last_header is not None
            dt = header_time - self.last_header
            self.header_dts.append(dt)
            if dt <= 0:
                self.non_monotonic += 1

        self.last_header = header_time
        self.last_bag = bag_time
        self.count += 1
        self.timestamps.append(header_time)
        self.bag_header_offsets.append(bag_time - header_time)

        if is_image_msg(msg):
            width = getattr(msg, "width", None)
            height = getattr(msg, "height", None)
            if width is not None and height is not None:
                key = f"{int(width)}x{int(height)}"
                self.image_shapes[key] = self.image_shapes.get(key, 0) + 1
            encoding = getattr(msg, "encoding", None) or getattr(msg, "format", None)
            if encoding:
                self.image_encodings[str(encoding)] = self.image_encodings.get(str(encoding), 0) + 1

        if is_imu_msg(msg):
            av = getattr(msg, "angular_velocity", None)
            la = getattr(msg, "linear_acceleration", None)
            if av is not None:
                self.gyro.append((float(av.x), float(av.y), float(av.z)))
            if la is not None:
                self.accel.append((float(la.x), float(la.y), float(la.z)))

    @property
    def duration(self) -> float:
        if self.first_header is None or self.last_header is None:
            return 0.0
        return max(0.0, self.last_header - self.first_header)

    @property
    def hz(self) -> float:
        if self.count < 2 or self.duration <= 0:
            return 0.0
        return (self.count - 1) / self.duration

    def percentile_dt(self, p: float) -> Optional[float]:
        positive = [dt for dt in self.header_dts if dt > 0]
        return percentile(positive, p) if positive else None

    def max_dt(self) -> Optional[float]:
        positive = [dt for dt in self.header_dts if dt > 0]
        return max(positive) if positive else None

    def offset_abs_p95(self) -> Optional[float]:
        offsets = [abs(x) for x in self.bag_header_offsets]
        return percentile(offsets, 95.0) if offsets else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "type": self.msg_type,
            "count": self.count,
            "duration": self.duration,
            "hz": self.hz,
            "dt_median": self.percentile_dt(50.0),
            "dt_p95": self.percentile_dt(95.0),
            "dt_max": self.max_dt(),
            "non_monotonic": self.non_monotonic,
            "bag_header_offset_abs_p95": self.offset_abs_p95(),
            "image_shapes": self.image_shapes,
            "image_encodings": self.image_encodings,
            "imu": imu_summary(self),
        }


@dataclass
class Report:
    bag: str
    image_topics: List[str]
    imu_topics: List[str]
    topics: Dict[str, TopicStats]
    checks: List[Check] = field(default_factory=list)
    target: Dict[str, Any] = field(default_factory=dict)

    def add(self, level: str, name: str, message: str) -> None:
        self.checks.append(Check(level, name, message))

    def status(self) -> str:
        if any(c.level == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.level == "WARN" for c in self.checks):
            return "WARN"
        return "PASS"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bag": self.bag,
            "status": self.status(),
            "image_topics": self.image_topics,
            "imu_topics": self.imu_topics,
            "topics": {name: stats.to_dict() for name, stats in self.topics.items()},
            "target": clean_hidden(self.target),
            "checks": [c.__dict__ for c in self.checks],
        }


def get_time_sec(stamp: Any) -> float:
    if hasattr(stamp, "to_sec"):
        return float(stamp.to_sec())
    if hasattr(stamp, "toSec"):
        return float(stamp.toSec())
    secs = getattr(stamp, "secs", None)
    nsecs = getattr(stamp, "nsecs", None)
    if secs is not None and nsecs is not None:
        return float(secs) + float(nsecs) * 1e-9
    return float(stamp)


def get_header_time(msg: Any, default: float) -> float:
    header = getattr(msg, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return default
    return get_time_sec(stamp)


def is_image_msg(msg: Any) -> bool:
    return getattr(msg, "_type", "") in IMAGE_TYPES


def is_imu_msg(msg: Any) -> bool:
    return getattr(msg, "_type", "") in IMU_TYPES


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    return float(np.percentile(arr, pct))


def fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6g}s"


def fmt_hz(value: float) -> str:
    return f"{value:.3f}Hz"


def progress(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "quiet", False):
        return
    print(f"[check_bag] {message}", file=sys.stderr, flush=True)


class ProgressTicker:
    def __init__(self, args: argparse.Namespace, label: str, total: Optional[int] = None):
        self.args = args
        self.label = label
        self.total = total
        self.start = time.monotonic()
        self.last = 0.0
        progress(args, self._format(0, "started"))

    def update(self, done: int, extra: str = "") -> None:
        if getattr(self.args, "quiet", False):
            return
        now = time.monotonic()
        interval = max(0.1, float(getattr(self.args, "progress_interval", 2.0)))
        if now - self.last < interval:
            return
        self.last = now
        progress(self.args, self._format(done, extra))

    def done(self, done: int, extra: str = "") -> None:
        progress(self.args, self._format(done, extra or "done"))

    def _format(self, done: int, extra: str) -> str:
        elapsed = max(1e-9, time.monotonic() - self.start)
        rate = done / elapsed
        if self.total:
            pct = min(100.0, 100.0 * done / self.total)
            base = f"{self.label}: {done}/{self.total} ({pct:.1f}%, {rate:.1f}/s)"
        else:
            base = f"{self.label}: {done} ({rate:.1f}/s)"
        return f"{base} {extra}".rstrip()


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"YAML file does not contain a mapping: {path}")
    return data


def target_config(path: str) -> Dict[str, Any]:
    data = load_yaml(path)
    if "target_type" not in data:
        raise RuntimeError(f"target YAML is missing target_type: {path}")
    return data


def topics_from_camchain(path: Optional[str]) -> List[str]:
    if not path:
        return []
    data = load_yaml(path)
    topics = []
    for key in sorted(data):
        if key.startswith("cam") and isinstance(data[key], dict):
            topic = data[key].get("rostopic")
            if topic:
                topics.append(str(topic))
    return topics


def topics_from_imu_yamls(paths: Sequence[str]) -> Tuple[List[str], Dict[str, float]]:
    topics = []
    update_rates = {}
    for path in paths:
        data = load_yaml(path)
        topic = data.get("rostopic")
        if topic:
            topic = str(topic)
            topics.append(topic)
            rate = data.get("update_rate")
            if rate is not None:
                update_rates[topic] = float(rate)
    return topics, update_rates


def parse_topic_list(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    topics = []
    for value in values:
        topics.extend(part for part in value.split(",") if part)
    return topics


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def clean_hidden(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_hidden(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, list):
        return [clean_hidden(item) for item in value]
    return value


def import_rosbag() -> Any:
    try:
        import rosbag  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Python module 'rosbag' is not available. Run this tool inside a ROS1/Kalibr "
            "environment, for example after sourcing the workspace setup.bash."
        ) from exc
    return rosbag


def bag_topic_info(bag: Any) -> Dict[str, Dict[str, Any]]:
    info = bag.get_type_and_topic_info()
    topics = getattr(info, "topics", {})
    result = {}
    for name, item in topics.items():
        msg_type = getattr(item, "msg_type", None) or getattr(item, "datatype", "unknown")
        count = getattr(item, "message_count", None)
        result[name] = {"type": msg_type, "count": count}
    return result


def autodiscover_topics(topic_info: Dict[str, Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    images = []
    imus = []
    for topic, meta in topic_info.items():
        msg_type = meta.get("type")
        if msg_type in IMAGE_TYPES:
            images.append(topic)
        if msg_type in IMU_TYPES:
            imus.append(topic)
    return images, imus


def collect_bag_stats(args: argparse.Namespace) -> Tuple[Report, Dict[str, float]]:
    rosbag = import_rosbag()
    image_topics = unique_keep_order(parse_topic_list(args.image_topics) + topics_from_camchain(args.cams))
    imu_topics, imu_update_rates = topics_from_imu_yamls(args.imu or [])
    imu_topics = unique_keep_order(parse_topic_list(args.imu_topics) + imu_topics)

    with rosbag.Bag(args.bag, "r") as bag:
        topic_info = bag_topic_info(bag)
        auto_images, auto_imus = autodiscover_topics(topic_info)
        progress(args, f"Opened bag: {args.bag}")
        progress(args, f"Discovered {len(topic_info)} topics, {len(auto_images)} image topics, {len(auto_imus)} IMU topics")
        if args.autodiscover or (not image_topics and not imu_topics):
            image_topics = unique_keep_order(image_topics + auto_images)
            imu_topics = unique_keep_order(imu_topics + auto_imus)

        requested_topics = unique_keep_order(image_topics + imu_topics)
        report = Report(args.bag, image_topics, imu_topics, {})
        progress(args, f"Selected image topics: {image_topics if image_topics else 'none'}")
        progress(args, f"Selected IMU topics: {imu_topics if imu_topics else 'none'}")

        for topic in requested_topics:
            if topic not in topic_info:
                report.add("FAIL", "missing_topic", f"{topic}: not present in bag")
                progress(args, f"Missing requested topic: {topic}")

        present_topics = [topic for topic in requested_topics if topic in topic_info]
        stats = {topic: TopicStats(topic=topic, msg_type=topic_info[topic].get("type", "unknown")) for topic in present_topics}
        for topic in present_topics:
            meta = topic_info[topic]
            progress(args, f"Topic {topic}: {meta.get('type', 'unknown')}, {meta.get('count', 'unknown')} messages")
        if not present_topics:
            progress(args, "No selected topics are present; skipping message scan")
            report.topics = stats
            return report, imu_update_rates

        total_expected = sum(meta.get("count") or 0 for topic, meta in topic_info.items() if topic in present_topics)
        ticker = ProgressTicker(args, "Scanning selected bag messages", total_expected or None)
        scanned = 0
        for topic, msg, stamp in bag.read_messages(topics=present_topics):
            scanned += 1
            stats[topic].add(msg, get_time_sec(stamp))
            ticker.update(scanned, f"current={topic}")
        ticker.done(scanned)

        report.topics = stats
        return report, imu_update_rates


def imu_summary(stats: TopicStats) -> Optional[Dict[str, Any]]:
    if not stats.gyro and not stats.accel:
        return None
    out: Dict[str, Any] = {}
    if stats.gyro:
        gyro = np.asarray(stats.gyro, dtype=float)
        gyro_norm = np.linalg.norm(gyro, axis=1)
        out["gyro_axis_std"] = np.std(gyro, axis=0).tolist()
        out["gyro_norm_p95"] = float(np.percentile(gyro_norm, 95.0))
        out["gyro_norm_max"] = float(np.max(gyro_norm))
    if stats.accel:
        accel = np.asarray(stats.accel, dtype=float)
        accel_norm = np.linalg.norm(accel, axis=1)
        out["accel_axis_std"] = np.std(accel, axis=0).tolist()
        out["accel_norm_mean"] = float(np.mean(accel_norm))
        out["accel_norm_std"] = float(np.std(accel_norm))
        out["accel_norm_p95"] = float(np.percentile(accel_norm, 95.0))
    return out


def evaluate_report(report: Report, args: argparse.Namespace, imu_update_rates: Dict[str, float]) -> None:
    all_requested = report.image_topics + report.imu_topics
    for topic in all_requested:
        stats = report.topics.get(topic)
        if stats is None:
            continue
        if stats.count == 0:
            report.add("FAIL", "empty_topic", f"{topic}: no messages")
            continue
        report.add("INFO", "topic_summary", topic_summary(stats))
        if stats.non_monotonic:
            report.add("FAIL", "non_monotonic", f"{topic}: {stats.non_monotonic} non-monotonic header timestamps")

        offset_p95 = stats.offset_abs_p95()
        if offset_p95 is not None and offset_p95 > args.max_header_bag_offset:
            report.add(
                "WARN",
                "header_bag_offset",
                f"{topic}: p95 |bag_time - header_time| is {fmt_seconds(offset_p95)}",
            )

        max_dt = stats.max_dt()
        median_dt = stats.percentile_dt(50.0)
        if max_dt is not None and median_dt is not None and max_dt > args.max_gap_ratio * median_dt:
            report.add(
                "WARN",
                "large_gap",
                f"{topic}: max dt {fmt_seconds(max_dt)} exceeds {args.max_gap_ratio:.1f}x median dt {fmt_seconds(median_dt)}",
            )

    evaluate_dataset_duration(report, args)
    evaluate_images(report, args)
    evaluate_imus(report, args, imu_update_rates)
    evaluate_overlap(report, args)
    evaluate_camera_sync(report, args)


def topic_summary(stats: TopicStats) -> str:
    pieces = [
        f"{stats.topic}: {stats.count} msgs",
        stats.msg_type,
        f"duration {fmt_seconds(stats.duration)}",
        fmt_hz(stats.hz),
        f"median dt {fmt_seconds(stats.percentile_dt(50.0))}",
        f"p95 dt {fmt_seconds(stats.percentile_dt(95.0))}",
    ]
    if stats.image_shapes:
        pieces.append(f"shapes {stats.image_shapes}")
    return ", ".join(pieces)


def evaluate_dataset_duration(report: Report, args: argparse.Namespace) -> None:
    spans = [stats.duration for stats in report.topics.values() if stats.count > 1]
    if not spans:
        return
    duration = min(spans)
    if duration < args.min_duration:
        report.add("WARN", "duration", f"common shortest topic duration {duration:.1f}s is below {args.min_duration:.1f}s")
    else:
        report.add("PASS", "duration", f"common shortest topic duration {duration:.1f}s")


def evaluate_images(report: Report, args: argparse.Namespace) -> None:
    for topic in report.image_topics:
        stats = report.topics.get(topic)
        if not stats:
            continue
        if stats.msg_type not in IMAGE_TYPES:
            report.add("WARN", "image_type", f"{topic}: expected image topic, got {stats.msg_type}")
        if stats.count < args.min_images:
            report.add("WARN", "image_count", f"{topic}: {stats.count} images is below {args.min_images}")
        else:
            report.add("PASS", "image_count", f"{topic}: {stats.count} images")
        if stats.hz < args.min_image_hz:
            report.add("WARN", "image_rate", f"{topic}: {fmt_hz(stats.hz)} is below {fmt_hz(args.min_image_hz)}")
        else:
            report.add("PASS", "image_rate", f"{topic}: {fmt_hz(stats.hz)}")
        if len(stats.image_shapes) > 1:
            report.add("WARN", "image_shape", f"{topic}: multiple image sizes observed {stats.image_shapes}")


def evaluate_imus(report: Report, args: argparse.Namespace, imu_update_rates: Dict[str, float]) -> None:
    for topic in report.imu_topics:
        stats = report.topics.get(topic)
        if not stats:
            continue
        if stats.msg_type not in IMU_TYPES:
            report.add("WARN", "imu_type", f"{topic}: expected sensor_msgs/Imu, got {stats.msg_type}")
        expected = imu_update_rates.get(topic, args.min_imu_hz)
        min_hz = expected * (1.0 - args.rate_tolerance) if topic in imu_update_rates else args.min_imu_hz
        if stats.hz < min_hz:
            report.add("WARN", "imu_rate", f"{topic}: {fmt_hz(stats.hz)} is below expected minimum {fmt_hz(min_hz)}")
        else:
            report.add("PASS", "imu_rate", f"{topic}: {fmt_hz(stats.hz)}")

        summary = imu_summary(stats)
        if not summary:
            continue
        gyro_p95 = summary.get("gyro_norm_p95")
        accel_std = summary.get("accel_norm_std")
        gyro_axis_std = summary.get("gyro_axis_std", [])
        active_axes = sum(1 for value in gyro_axis_std if value >= args.min_gyro_axis_std)
        if gyro_p95 is not None and gyro_p95 < args.min_gyro_p95:
            report.add("WARN", "imu_motion", f"{topic}: gyro norm p95 {gyro_p95:.4g} rad/s suggests weak rotation")
        else:
            report.add("PASS", "imu_motion", f"{topic}: gyro norm p95 {gyro_p95:.4g} rad/s")
        if accel_std is not None and accel_std < args.min_accel_norm_std:
            report.add("WARN", "imu_accel_motion", f"{topic}: accel norm std {accel_std:.4g} m/s^2 suggests weak acceleration variation")
        if active_axes < args.min_active_gyro_axes:
            report.add("WARN", "imu_axis_excitation", f"{topic}: only {active_axes} gyro axes exceed std {args.min_gyro_axis_std:g}")


def evaluate_overlap(report: Report, args: argparse.Namespace) -> None:
    stats = [s for s in report.topics.values() if s.count > 1 and s.first_header is not None and s.last_header is not None]
    if len(stats) < 2:
        return
    overlap_start = max(s.first_header for s in stats if s.first_header is not None)
    overlap_end = min(s.last_header for s in stats if s.last_header is not None)
    union_start = min(s.first_header for s in stats if s.first_header is not None)
    union_end = max(s.last_header for s in stats if s.last_header is not None)
    overlap = max(0.0, overlap_end - overlap_start)
    union = max(0.0, union_end - union_start)
    ratio = overlap / union if union > 0 else 0.0
    if ratio < args.min_overlap_ratio:
        report.add("WARN", "time_overlap", f"topic time overlap ratio {ratio:.3f} is below {args.min_overlap_ratio:.3f}")
    else:
        report.add("PASS", "time_overlap", f"topic time overlap ratio {ratio:.3f}")


def evaluate_camera_sync(report: Report, args: argparse.Namespace) -> None:
    if len(report.image_topics) < 2:
        return
    ref_topic = report.image_topics[0]
    ref = report.topics.get(ref_topic)
    if not ref:
        return
    threshold = args.max_camera_sync_ms * 1e-3
    for topic in report.image_topics[1:]:
        stats = report.topics.get(topic)
        if not stats:
            continue
        distances = nearest_distances(ref.timestamps, stats.timestamps)
        if not distances:
            continue
        med = percentile(distances, 50.0)
        p95 = percentile(distances, 95.0)
        if med is not None and p95 is not None and p95 > threshold:
            report.add(
                "WARN",
                "camera_sync",
                f"{ref_topic} vs {topic}: nearest timestamp median {med * 1e3:.2f}ms, p95 {p95 * 1e3:.2f}ms",
            )
        else:
            report.add("PASS", "camera_sync", f"{ref_topic} vs {topic}: p95 nearest timestamp {p95 * 1e3:.2f}ms")


def nearest_distances(a: Sequence[float], b: Sequence[float]) -> List[float]:
    if not a or not b:
        return []
    b_sorted = sorted(b)
    distances = []
    for value in a:
        pos = bisect.bisect_left(b_sorted, value)
        candidates = []
        if pos < len(b_sorted):
            candidates.append(abs(b_sorted[pos] - value))
        if pos > 0:
            candidates.append(abs(b_sorted[pos - 1] - value))
        if candidates:
            distances.append(min(candidates))
    return distances


def run_target_checks(report: Report, args: argparse.Namespace) -> None:
    if not args.target:
        return
    try:
        if args.cams:
            target_result = detect_targets(args)
        else:
            target_result = detect_targets_without_intrinsics(report, args)
    except Exception as exc:
        report.add("WARN", "target_detection", f"target checks skipped: {exc}")
        return
    report.target = target_result
    for cam_name, data in target_result.get("cameras", {}).items():
        processed = data.get("processed", 0)
        detected = data.get("detected", 0)
        ratio = detected / processed if processed else 0.0
        coverage = data.get("center_grid_coverage", 0.0)
        corner_coverage = data.get("corner_grid_coverage")
        edge_sides = data.get("edge_sides_covered")
        roll_bins = data.get("roll_bins_covered")
        tilt_sides = data.get("tilt_sides_covered")
        area_ratio = data.get("bbox_area_p90_p10_ratio")
        if detected < args.min_target_observations:
            report.add("WARN", "target_observations", f"{cam_name}: {detected} detections below {args.min_target_observations}")
        else:
            report.add("PASS", "target_observations", f"{cam_name}: {detected}/{processed} target detections")
        if ratio < args.min_target_detection_ratio:
            report.add("WARN", "target_detection_ratio", f"{cam_name}: detection ratio {ratio:.3f}")
        if coverage < args.min_target_center_coverage:
            report.add("WARN", "target_coverage", f"{cam_name}: target center grid coverage {coverage:.3f}")
        else:
            report.add("PASS", "target_coverage", f"{cam_name}: target center grid coverage {coverage:.3f}")
        if corner_coverage is not None:
            if corner_coverage < args.min_target_corner_coverage:
                report.add("WARN", "target_corner_coverage", f"{cam_name}: checkerboard corner grid coverage {corner_coverage:.3f}")
            else:
                report.add("PASS", "target_corner_coverage", f"{cam_name}: checkerboard corner grid coverage {corner_coverage:.3f}")
        if edge_sides is not None:
            if edge_sides < args.min_target_edge_sides:
                report.add(
                    "WARN",
                    "target_edge_coverage",
                    f"{cam_name}: checkerboard reached {edge_sides}/4 image sides; expected at least {args.min_target_edge_sides}",
                )
            else:
                report.add("PASS", "target_edge_coverage", f"{cam_name}: checkerboard reached {edge_sides}/4 image sides")
        if roll_bins is not None:
            if roll_bins < args.min_target_roll_bins:
                report.add(
                    "WARN",
                    "target_roll_coverage",
                    f"{cam_name}: checkerboard roll covered {roll_bins}/{args.target_roll_bins} angle bins",
                )
            else:
                report.add("PASS", "target_roll_coverage", f"{cam_name}: checkerboard roll covered {roll_bins}/{args.target_roll_bins} angle bins")
        if tilt_sides is not None:
            if tilt_sides < args.min_target_tilt_sides:
                report.add(
                    "WARN",
                    "target_tilt_coverage",
                    f"{cam_name}: perspective tilt covered {tilt_sides}/4 directions; expected at least {args.min_target_tilt_sides}",
                )
            else:
                report.add("PASS", "target_tilt_coverage", f"{cam_name}: perspective tilt covered {tilt_sides}/4 directions")
        if area_ratio is not None:
            if area_ratio < args.min_target_area_ratio:
                report.add("WARN", "target_scale_coverage", f"{cam_name}: board apparent area p90/p10 ratio {area_ratio:.2f}")
            else:
                report.add("PASS", "target_scale_coverage", f"{cam_name}: board apparent area p90/p10 ratio {area_ratio:.2f}")


def detect_targets_without_intrinsics(report: Report, args: argparse.Namespace) -> Dict[str, Any]:
    cfg = target_config(args.target)
    target_type = cfg.get("target_type")
    if target_type != "checkerboard":
        raise RuntimeError(
            "--target without --cams currently supports checkerboard only. "
            "Use --cams for Kalibr aprilgrid/circlegrid/charuco detection."
        )
    if not report.image_topics:
        raise RuntimeError("no image topics were selected or autodiscovered")

    cols = int(cfg["targetCols"])
    rows = int(cfg["targetRows"])
    rosbag = import_rosbag()
    max_images = args.target_max_images if args.target_max_images > 0 else None
    result: Dict[str, Any] = {
        "target": args.target,
        "detector": "opencv_checkerboard",
        "cameras": {},
    }
    per_topic = {
        topic: {
            "processed": 0,
            "detected": 0,
            "decode_failures": 0,
            "corners_counts": [],
            "centers": [],
            "corner_points": [],
            "bbox_areas": [],
            "roll_angles": [],
            "edge_sides": set(),
            "tilt_sides": set(),
            "last_time": None,
        }
        for topic in report.image_topics
    }

    total_scan = sum((report.topics.get(topic).count if report.topics.get(topic) else 0) for topic in report.image_topics)
    progress(args, f"Checkerboard target: {cols}x{rows} inner corners from {args.target}")
    workers = max(1, int(args.target_workers))
    max_pending = max(1, workers * 2)
    progress(args, f"Running OpenCV checkerboard detection on {len(report.image_topics)} image topics with {workers} worker threads")
    ticker = ProgressTicker(args, "Detecting checkerboards", total_scan or None)
    scanned = 0
    submitted = 0
    completed = 0
    futures: Dict[Future, str] = {}

    def collect_done(done: Iterable[Future]) -> None:
        nonlocal completed
        for future in done:
            topic_name = futures.pop(future)
            apply_checkerboard_result(per_topic[topic_name], future.result())
            completed += 1

    with rosbag.Bag(args.bag, "r") as bag, ThreadPoolExecutor(max_workers=workers) as executor:
        for topic, msg, stamp in bag.read_messages(topics=report.image_topics):
            scanned += 1
            ticker.update(scanned, f"current={topic}, submitted={submitted}, done={completed}, pending={len(futures)}")
            state = per_topic[topic]
            if max_images is not None and state["processed"] >= max_images:
                continue
            stamp_sec = get_header_time(msg, get_time_sec(stamp))
            last_time = state["last_time"]
            if args.bag_freq and last_time is not None and (stamp_sec - last_time) < 1.0 / args.bag_freq:
                continue
            state["last_time"] = stamp_sec
            state["processed"] += 1
            future = executor.submit(
                process_checkerboard_message,
                msg,
                cols,
                rows,
                args.target_edge_margin,
                args.target_tilt_ratio,
            )
            futures[future] = topic
            submitted += 1
            if len(futures) >= max_pending:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                collect_done(done)
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            collect_done(done)
            ticker.update(scanned, f"submitted={submitted}, done={completed}, pending={len(futures)}")
    ticker.done(scanned, f"submitted={submitted}, done={completed}")

    for topic, state in per_topic.items():
        processed = int(state["processed"])
        detected = int(state["detected"])
        progress(
            args,
            f"Checkerboard summary {topic}: processed={processed}, detected={detected}, "
            f"decode_failures={int(state['decode_failures'])}",
        )
        result["cameras"][topic] = {
            "processed": processed,
            "detected": detected,
            "decode_failures": int(state["decode_failures"]),
            "detection_ratio": detected / processed if processed else 0.0,
            "corners_median": percentile(state["corners_counts"], 50.0),
            "bbox_area_p10": percentile(state["bbox_areas"], 10.0),
            "bbox_area_p90": percentile(state["bbox_areas"], 90.0),
            "bbox_area_p90_p10_ratio": ratio_or_none(percentile(state["bbox_areas"], 90.0), percentile(state["bbox_areas"], 10.0)),
            "center_grid_coverage": grid_coverage(state["centers"], args.target_grid),
            "corner_grid_coverage": grid_coverage(state["corner_points"], args.target_grid),
            "edge_sides": sorted(state["edge_sides"]),
            "edge_sides_covered": len(state["edge_sides"]),
            "roll_angle_min_deg": percentile(state["roll_angles"], 0.0),
            "roll_angle_max_deg": percentile(state["roll_angles"], 100.0),
            "roll_bins_covered": angle_bin_coverage(state["roll_angles"], args.target_roll_bins),
            "tilt_sides": sorted(state["tilt_sides"]),
            "tilt_sides_covered": len(state["tilt_sides"]),
            "_plot": {
                "centers": state["centers"],
                "corner_points": state["corner_points"],
                "bbox_areas": state["bbox_areas"],
                "roll_angles": state["roll_angles"],
            },
        }
    return result


def process_checkerboard_message(
    msg: Any,
    cols: int,
    rows: int,
    edge_margin: float,
    tilt_ratio: float,
) -> Dict[str, Any]:
    try:
        image = image_msg_to_gray(msg)
        corners = find_checkerboard(image, cols, rows)
        if corners is None:
            return {"decode_failure": False, "detected": False}
        metrics = checkerboard_observation_metrics(
            corners,
            image.shape[1],
            image.shape[0],
            cols,
            rows,
            edge_margin,
            tilt_ratio,
        )
        return {
            "decode_failure": False,
            "detected": True,
            "corners_count": int(corners.shape[0]),
            "metrics": metrics,
        }
    except Exception as exc:
        return {"decode_failure": True, "error": str(exc)}


def apply_checkerboard_result(state: Dict[str, Any], result: Dict[str, Any]) -> None:
    if result.get("decode_failure"):
        state["decode_failures"] += 1
        return
    if not result.get("detected"):
        return
    metrics = result["metrics"]
    state["detected"] += 1
    state["corners_counts"].append(result["corners_count"])
    state["centers"].append(metrics["center"])
    state["corner_points"].extend(metrics["corner_points"])
    state["bbox_areas"].append(metrics["bbox_area"])
    state["roll_angles"].append(metrics["roll_angle_deg"])
    state["edge_sides"].update(metrics["edge_sides"])
    state["tilt_sides"].update(metrics["tilt_sides"])


def ratio_or_none(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 1e-12:
        return None
    return numerator / denominator


def checkerboard_observation_metrics(
    corners: np.ndarray,
    width: int,
    height: int,
    cols: int,
    rows: int,
    edge_margin: float,
    tilt_ratio: float,
) -> Dict[str, Any]:
    normalized = np.asarray(corners, dtype=float).reshape((-1, 2))
    norm = normalized.copy()
    norm[:, 0] /= float(width)
    norm[:, 1] /= float(height)
    center = np.mean(norm, axis=0)
    min_xy = np.min(norm, axis=0)
    max_xy = np.max(norm, axis=0)
    bbox_area = max(0.0, float(max_xy[0] - min_xy[0])) * max(0.0, float(max_xy[1] - min_xy[1]))
    edge_sides = set()
    if min_xy[0] <= edge_margin:
        edge_sides.add("left")
    if max_xy[0] >= 1.0 - edge_margin:
        edge_sides.add("right")
    if min_xy[1] <= edge_margin:
        edge_sides.add("top")
    if max_xy[1] >= 1.0 - edge_margin:
        edge_sides.add("bottom")

    grid = normalized.reshape((rows, cols, 2))
    row_vec = grid[0, -1] - grid[0, 0]
    roll_angle = math.degrees(math.atan2(float(row_vec[1]), float(row_vec[0]))) % 180.0

    top = segment_length(grid[0, 0], grid[0, -1])
    bottom = segment_length(grid[-1, 0], grid[-1, -1])
    left = segment_length(grid[0, 0], grid[-1, 0])
    right = segment_length(grid[0, -1], grid[-1, -1])
    tilt_sides = set()
    if top > 0 and bottom > 0:
        ratio = top / bottom
        if ratio >= tilt_ratio:
            tilt_sides.add("bottom")
        elif ratio <= 1.0 / tilt_ratio:
            tilt_sides.add("top")
    if left > 0 and right > 0:
        ratio = left / right
        if ratio >= tilt_ratio:
            tilt_sides.add("right")
        elif ratio <= 1.0 / tilt_ratio:
            tilt_sides.add("left")

    return {
        "center": (float(center[0]), float(center[1])),
        "corner_points": [(float(x), float(y)) for x, y in norm],
        "bbox_area": bbox_area,
        "edge_sides": edge_sides,
        "roll_angle_deg": roll_angle,
        "tilt_sides": tilt_sides,
    }


def segment_length(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def angle_bin_coverage(angles_deg: Sequence[float], bins: int) -> int:
    if not angles_deg or bins <= 0:
        return 0
    occupied = set()
    for angle in angles_deg:
        normalized = float(angle) % 180.0
        occupied.add(min(bins - 1, int(normalized / 180.0 * bins)))
    return len(occupied)


def image_msg_to_gray(msg: Any) -> np.ndarray:
    import cv2  # type: ignore

    msg_type = getattr(msg, "_type", "")
    if msg_type == "sensor_msgs/CompressedImage":
        data = np.frombuffer(msg.data, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise RuntimeError("cv2.imdecode returned None")
        return image

    if msg_type == "sensor_msgs/Image":
        width = int(msg.width)
        height = int(msg.height)
        encoding = str(getattr(msg, "encoding", "")).lower()
        data = msg.data
        if encoding in {"mono8", "8uc1"}:
            return np.frombuffer(data, dtype=np.uint8).reshape((height, width))
        if encoding in {"mono16", "16uc1"}:
            image16 = np.frombuffer(data, dtype=np.uint16).reshape((height, width))
            return (image16 / 256).astype(np.uint8)
        if encoding in {"bgr8", "8uc3"}:
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 3))
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if encoding == "rgb8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 3))
            return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        if encoding in {"bgra8", "8uc4"}:
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 4))
            return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        if encoding == "rgba8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 4))
            return cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
        if encoding == "bayer_rggb8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
            return cv2.cvtColor(image, cv2.COLOR_BAYER_BG2GRAY)
        if encoding == "bayer_bggr8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
            return cv2.cvtColor(image, cv2.COLOR_BAYER_RG2GRAY)
        if encoding == "bayer_gbrg8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
            return cv2.cvtColor(image, cv2.COLOR_BAYER_GR2GRAY)
        if encoding == "bayer_grbg8":
            image = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
            return cv2.cvtColor(image, cv2.COLOR_BAYER_GB2GRAY)
        raise RuntimeError(f"unsupported sensor_msgs/Image encoding: {encoding}")

    raise RuntimeError(f"unsupported image message type: {msg_type}")


def find_checkerboard(image: np.ndarray, cols: int, rows: int) -> Optional[np.ndarray]:
    import cv2  # type: ignore

    pattern_size = (cols, rows)
    if hasattr(cv2, "findChessboardCornersSB"):
        ok, corners = cv2.findChessboardCornersSB(image, pattern_size)
    else:
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
        ok, corners = cv2.findChessboardCorners(image, pattern_size, flags)
        if ok:
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01)
            corners = cv2.cornerSubPix(image, corners, (5, 5), (-1, -1), criteria)
    if not ok:
        return None
    return np.asarray(corners, dtype=float).reshape((-1, 2))


def detect_targets(args: argparse.Namespace) -> Dict[str, Any]:
    kalibr_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "aslam_offline_calibration", "kalibr", "python"))
    if kalibr_python not in sys.path:
        sys.path.insert(0, kalibr_python)

    import cv2  # type: ignore
    import kalibr_common as kc  # type: ignore
    import kalibr_camera_calibration as kcc  # type: ignore

    chain = kc.CameraChainParameters(args.cams)
    target = kc.CalibrationTargetParameters(args.target)
    result: Dict[str, Any] = {"target": args.target, "cameras": {}}
    max_images = args.target_max_images if args.target_max_images > 0 else None
    progress(args, f"Running Kalibr target detection for {chain.numCameras()} cameras")

    for cam_idx in range(chain.numCameras()):
        cam_params = chain.getCameraParameters(cam_idx)
        topic = cam_params.getRosTopic()
        progress(args, f"Preparing Kalibr detector for cam{cam_idx}: {topic}")
        camera = kc.AslamCamera.fromParameters(cam_params)
        detector = kcc.TargetDetector(target, camera.geometry, showCorners=args.show_extraction).detector
        dataset = kc.BagImageDatasetReader(args.bag, topic, bag_freq=args.bag_freq)
        width, height = cam_params.getResolution()

        processed = 0
        detected = 0
        corners_counts: List[int] = []
        centers: List[Tuple[float, float]] = []
        corner_points: List[Tuple[float, float]] = []
        bbox_areas: List[float] = []
        roll_angles: List[float] = []
        total = dataset.numImages()
        if max_images is not None:
            total = min(total, max_images)
        ticker = ProgressTicker(args, f"Kalibr target detection cam{cam_idx}", total or None)
        for timestamp, image in dataset.readDataset():
            if max_images is not None and processed >= max_images:
                break
            processed += 1
            ticker.update(processed, f"detected={detected}")
            success, obs = detector.findTarget(timestamp, np.array(image))
            if not success:
                continue
            detected += 1
            corners = normalize_corners(obs.getCornersImageFrame())
            if corners.size:
                corners_counts.append(int(corners.shape[0]))
                center = np.mean(corners, axis=0)
                centers.append((float(center[0]) / float(width), float(center[1]) / float(height)))
                norm_corners = corners.copy()
                norm_corners[:, 0] /= float(width)
                norm_corners[:, 1] /= float(height)
                corner_points.extend((float(x), float(y)) for x, y in norm_corners)
                if corners.shape[0] >= 2:
                    row_vec = corners[-1] - corners[0]
                    roll_angles.append(math.degrees(math.atan2(float(row_vec[1]), float(row_vec[0]))) % 180.0)
                min_xy = np.min(corners, axis=0)
                max_xy = np.max(corners, axis=0)
                area = max(0.0, float(max_xy[0] - min_xy[0])) * max(0.0, float(max_xy[1] - min_xy[1]))
                bbox_areas.append(area / float(width * height))
            obs.clearImage()
        ticker.done(processed, f"detected={detected}")
        progress(args, f"Kalibr target summary cam{cam_idx}: processed={processed}, detected={detected}")

        result["cameras"][f"cam{cam_idx}:{topic}"] = {
            "processed": processed,
            "detected": detected,
            "detection_ratio": detected / processed if processed else 0.0,
            "corners_median": percentile(corners_counts, 50.0),
            "bbox_area_p10": percentile(bbox_areas, 10.0),
            "bbox_area_p90": percentile(bbox_areas, 90.0),
            "center_grid_coverage": grid_coverage(centers, args.target_grid),
            "_plot": {
                "centers": centers,
                "corner_points": corner_points,
                "bbox_areas": bbox_areas,
                "roll_angles": roll_angles,
            },
        }
    cv2.destroyAllWindows()
    return result


def normalize_corners(corners: Any) -> np.ndarray:
    arr = np.asarray(corners, dtype=float)
    if arr.size == 0:
        return np.zeros((0, 2), dtype=float)
    if arr.ndim != 2:
        return arr.reshape((-1, 2))
    if arr.shape[0] == 2 and arr.shape[1] != 2:
        arr = arr.T
    if arr.shape[1] > 2:
        arr = arr[:, :2]
    return arr


def grid_coverage(points: Sequence[Tuple[float, float]], grid_size: int) -> float:
    if not points:
        return 0.0
    occupied = set()
    for x, y in points:
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        ix = min(grid_size - 1, max(0, int(x * grid_size)))
        iy = min(grid_size - 1, max(0, int(y * grid_size)))
        occupied.add((ix, iy))
    return len(occupied) / float(grid_size * grid_size)


def safe_filename(value: str) -> str:
    cleaned = []
    for char in value.strip("/"):
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned) or "topic"


def public_camera_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if not str(key).startswith("_")}


def generate_visual_report(report: Report, args: argparse.Namespace) -> None:
    if not args.report_dir:
        return
    os.makedirs(args.report_dir, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/check_bag_mpl")
    os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    progress(args, f"Writing visual report to {args.report_dir}")
    with open(os.path.join(args.report_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, sort_keys=True)

    figures: Dict[str, List[str]] = {}
    for name, data in report.target.get("cameras", {}).items():
        plot = data.get("_plot", {})
        if not plot:
            continue
        topic_slug = safe_filename(name)
        figures[name] = []
        centers_path = os.path.join(args.report_dir, f"{topic_slug}_centers.png")
        plot_points(
            plt,
            plot.get("centers", []),
            centers_path,
            "Checkerboard center coverage",
            args.target_grid,
            point_size=24,
        )
        figures[name].append(os.path.basename(centers_path))

        corners_path = os.path.join(args.report_dir, f"{topic_slug}_corners.png")
        plot_points(
            plt,
            plot.get("corner_points", []),
            corners_path,
            "Detected checkerboard corner coverage",
            args.target_grid,
            point_size=2,
        )
        figures[name].append(os.path.basename(corners_path))

        hist_path = os.path.join(args.report_dir, f"{topic_slug}_hist.png")
        plot_histograms(
            plt,
            plot.get("bbox_areas", []),
            plot.get("roll_angles", []),
            hist_path,
        )
        figures[name].append(os.path.basename(hist_path))

    html_path = os.path.join(args.report_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html_report(report, figures))
    progress(args, f"Visual report written: {html_path}")


def plot_points(
    plt: Any,
    points: Sequence[Tuple[float, float]],
    path: str,
    title: str,
    grid_size: int,
    point_size: int,
) -> None:
    fig, ax = plt.subplots(figsize=(6, 4.5), dpi=140)
    ax.set_title(title)
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(1.0, 0.0)
    ax.set_xlabel("normalized image x")
    ax.set_ylabel("normalized image y")
    ax.set_aspect("equal", adjustable="box")
    for idx in range(1, grid_size):
        value = idx / float(grid_size)
        ax.axvline(value, color="#d0d0d0", linewidth=0.8)
        ax.axhline(value, color="#d0d0d0", linewidth=0.8)
    if points:
        arr = np.asarray(points, dtype=float)
        ax.scatter(arr[:, 0], arr[:, 1], s=point_size, c="#1f77b4", alpha=0.55, edgecolors="none")
    else:
        ax.text(0.5, 0.5, "no detections", ha="center", va="center")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_histograms(plt: Any, bbox_areas: Sequence[float], roll_angles: Sequence[float], path: str) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.4), dpi=140)
    axes[0].set_title("Board apparent area")
    axes[0].set_xlabel("normalized bbox area")
    axes[0].set_ylabel("count")
    if bbox_areas:
        axes[0].hist(bbox_areas, bins=24, color="#2ca02c", alpha=0.8)
    else:
        axes[0].text(0.5, 0.5, "no detections", ha="center", va="center", transform=axes[0].transAxes)

    axes[1].set_title("Image-plane board roll")
    axes[1].set_xlabel("degrees")
    axes[1].set_xlim(0.0, 180.0)
    if roll_angles:
        axes[1].hist(roll_angles, bins=np.linspace(0.0, 180.0, 19), color="#ff7f0e", alpha=0.8)
    else:
        axes[1].text(0.5, 0.5, "no detections", ha="center", va="center", transform=axes[1].transAxes)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def render_html_report(report: Report, figures: Dict[str, List[str]]) -> str:
    rows = []
    for name, data in report.target.get("cameras", {}).items():
        public = public_camera_data(data)
        rows.append(
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{public.get('processed', '')}</td>"
            f"<td>{public.get('detected', '')}</td>"
            f"<td>{float(public.get('detection_ratio', 0.0)):.3f}</td>"
            f"<td>{float(public.get('center_grid_coverage', 0.0)):.3f}</td>"
            f"<td>{float(public.get('corner_grid_coverage', 0.0)):.3f}</td>"
            f"<td>{public.get('edge_sides_covered', '')}</td>"
            f"<td>{public.get('roll_bins_covered', '')}</td>"
            f"<td>{public.get('tilt_sides_covered', '')}</td>"
            f"<td>{format_optional_float(public.get('bbox_area_p90_p10_ratio'))}</td>"
            "</tr>"
        )

    check_rows = [
        f"<tr class='{html.escape(check.level.lower())}'><td>{html.escape(check.level)}</td>"
        f"<td>{html.escape(check.name)}</td><td>{html.escape(check.message)}</td></tr>"
        for check in report.checks
    ]
    figure_sections = []
    for name, paths in figures.items():
        imgs = "\n".join(f"<img src='{html.escape(path)}' alt='{html.escape(path)}'>" for path in paths)
        figure_sections.append(f"<section><h2>{html.escape(name)}</h2><div class='figures'>{imgs}</div></section>")

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>check_bag report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #222; }}
    h1, h2 {{ margin-bottom: 8px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; font-size: 13px; }}
    th {{ background: #f2f2f2; }}
    .warn td {{ background: #fff4d6; }}
    .fail td {{ background: #ffe1df; }}
    .pass td {{ background: #eaf7ea; }}
    .info td {{ background: #eef5ff; }}
    .figures {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }}
    img {{ width: 100%; max-width: 620px; border: 1px solid #ddd; }}
    code {{ background: #f4f4f4; padding: 2px 4px; }}
  </style>
</head>
<body>
  <h1>check_bag report</h1>
  <p><b>Bag:</b> <code>{html.escape(report.bag)}</code></p>
  <p><b>Status:</b> {html.escape(report.status())}</p>
  <h2>Target Summary</h2>
  <table>
    <thead><tr><th>topic</th><th>processed</th><th>detected</th><th>ratio</th><th>center coverage</th><th>corner coverage</th><th>edge sides</th><th>roll bins</th><th>tilt sides</th><th>area p90/p10</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  {''.join(figure_sections)}
  <h2>Checks</h2>
  <table>
    <thead><tr><th>level</th><th>name</th><th>message</th></tr></thead>
    <tbody>{''.join(check_rows)}</tbody>
  </table>
</body>
</html>
"""


def format_optional_float(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def print_report(report: Report) -> None:
    print(f"Bag: {report.bag}")
    print(f"Status: {report.status()}")
    print("")
    for stats in report.topics.values():
        print(topic_summary(stats))
        summary = imu_summary(stats)
        if summary:
            print(f"  imu: {json.dumps(summary, sort_keys=True)}")
    if report.target:
        print("")
        print("Target detection:")
        for name, data in report.target.get("cameras", {}).items():
            print(f"  {name}: {json.dumps(public_camera_data(data), sort_keys=True)}")
    print("")
    print("Checks:")
    order = {"FAIL": 0, "WARN": 1, "PASS": 2, "INFO": 3}
    for check in sorted(report.checks, key=lambda c: (order.get(c.level, 99), c.name)):
        print(f"[{check.level}] {check.name}: {check.message}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pre-check a ROS1 bag before Kalibr calibration.")
    parser.add_argument("--bag", required=True, help="ROS1 bag file.")
    parser.add_argument("--cams", help="Kalibr camera chain YAML; camera rostopics are read from camN.rostopic.")
    parser.add_argument("--imu", nargs="*", help="Kalibr IMU YAML files; rostopic and update_rate are read when present.")
    parser.add_argument("--target", help="Kalibr target YAML. Enables optional target detection checks.")
    parser.add_argument("--image-topics", nargs="*", help="Image topics, comma or space separated.")
    parser.add_argument("--imu-topics", nargs="*", help="IMU topics, comma or space separated.")
    parser.add_argument("--autodiscover", action="store_true", help="Add image and IMU topics discovered by message type.")
    parser.add_argument("--bag-freq", type=float, help="Optional target-detection extraction frequency, passed to Kalibr reader.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report.")
    parser.add_argument("--report-dir", help="Write an HTML visual report and PNG plots to this directory.")
    parser.add_argument("--show-extraction", action="store_true", help="Show Kalibr target extraction windows.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages on stderr.")
    parser.add_argument("--progress-interval", type=float, default=2.0, help="Seconds between progress updates.")

    parser.add_argument("--min-duration", type=float, default=60.0)
    parser.add_argument("--min-images", type=int, default=80)
    parser.add_argument("--min-image-hz", type=float, default=2.0)
    parser.add_argument("--min-imu-hz", type=float, default=50.0)
    parser.add_argument("--rate-tolerance", type=float, default=0.20)
    parser.add_argument("--max-gap-ratio", type=float, default=3.0)
    parser.add_argument("--max-header-bag-offset", type=float, default=0.2)
    parser.add_argument("--min-overlap-ratio", type=float, default=0.80)
    parser.add_argument("--max-camera-sync-ms", type=float, default=5.0)
    parser.add_argument("--min-gyro-p95", type=float, default=0.25)
    parser.add_argument("--min-gyro-axis-std", type=float, default=0.01)
    parser.add_argument("--min-active-gyro-axes", type=int, default=2)
    parser.add_argument("--min-accel-norm-std", type=float, default=0.20)
    parser.add_argument("--target-max-images", type=int, default=0, help="Limit images per camera for target checks; 0 means all.")
    parser.add_argument("--target-workers", type=int, default=1, help="Worker threads for OpenCV checkerboard detection.")
    parser.add_argument("--min-target-observations", type=int, default=40)
    parser.add_argument("--min-target-detection-ratio", type=float, default=0.20)
    parser.add_argument("--target-grid", type=int, default=4)
    parser.add_argument("--min-target-center-coverage", type=float, default=0.35)
    parser.add_argument("--min-target-corner-coverage", type=float, default=0.45)
    parser.add_argument("--target-edge-margin", type=float, default=0.15, help="Normalized border band used for target edge coverage checks.")
    parser.add_argument("--min-target-edge-sides", type=int, default=4, help="Require the checkerboard to reach this many image sides.")
    parser.add_argument("--target-roll-bins", type=int, default=6, help="Number of 0-180 degree bins used for image-plane checkerboard roll coverage.")
    parser.add_argument("--min-target-roll-bins", type=int, default=4)
    parser.add_argument("--target-tilt-ratio", type=float, default=1.12, help="Opposite board edge length ratio considered a perspective tilt.")
    parser.add_argument("--min-target-tilt-sides", type=int, default=2)
    parser.add_argument("--min-target-area-ratio", type=float, default=2.0, help="Minimum p90/p10 apparent board area ratio.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        report, imu_update_rates = collect_bag_stats(args)
        evaluate_report(report, args, imu_update_rates)
        run_target_checks(report, args)
        generate_visual_report(report, args)
    except Exception as exc:
        print(f"check_bag failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print_report(report)
    return 1 if report.status() == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
