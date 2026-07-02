#!/usr/bin/env python3

import argparse
import os
import tempfile
import unittest

import numpy as np

from check_bag import (
    Check,
    Report,
    TopicStats,
    evaluate_camera_sync,
    evaluate_overlap,
    grid_coverage,
    nearest_distances,
    percentile,
)
from check_bag.check_bag import (
    angle_bin_coverage,
    checkerboard_observation_metrics,
    evaluate_target_result,
    find_checkerboard,
    generate_visual_report,
    scale_grid_coverage,
)


def args(**overrides):
    defaults = {
        "max_camera_sync_ms": 5.0,
        "min_overlap_ratio": 0.8,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class CheckBagPureLogicTest(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3], 50), 2.0)
        self.assertIsNone(percentile([], 50))

    def test_nearest_distances(self):
        distances = nearest_distances([0.0, 1.0, 2.0], [0.01, 1.02, 2.10])
        self.assertAlmostEqual(distances[0], 0.01)
        self.assertAlmostEqual(distances[1], 0.02)
        self.assertAlmostEqual(distances[2], 0.10)

    def test_overlap_warns_for_short_overlap(self):
        a = TopicStats(topic="/cam0")
        a.count = 2
        a.first_header = 0.0
        a.last_header = 10.0
        b = TopicStats(topic="/imu0")
        b.count = 2
        b.first_header = 8.0
        b.last_header = 12.0
        report = Report("x.bag", ["/cam0"], ["/imu0"], {"/cam0": a, "/imu0": b})

        evaluate_overlap(report, args(min_overlap_ratio=0.5))

        self.assertEqual(report.checks[0].level, "WARN")
        self.assertEqual(report.checks[0].name, "time_overlap")

    def test_camera_sync_passes_when_p95_is_small(self):
        a = TopicStats(topic="/cam0", count=3, timestamps=[0.0, 1.0, 2.0])
        b = TopicStats(topic="/cam1", count=3, timestamps=[0.001, 1.002, 2.003])
        report = Report("x.bag", ["/cam0", "/cam1"], [], {"/cam0": a, "/cam1": b})

        evaluate_camera_sync(report, args(max_camera_sync_ms=5.0))

        self.assertEqual(report.checks[0].level, "PASS")
        self.assertEqual(report.checks[0].name, "camera_sync")

    def test_grid_coverage(self):
        points = [(0.1, 0.1), (0.9, 0.9), (0.51, 0.51), (0.52, 0.52)]
        self.assertAlmostEqual(grid_coverage(points, 2), 2.0 / 4.0)

    def test_find_checkerboard_on_synthetic_image(self):
        cols = 8
        rows = 11
        square = 32
        image = np.zeros(((rows + 1) * square, (cols + 1) * square), dtype=np.uint8)
        for y in range(rows + 1):
            for x in range(cols + 1):
                if (x + y) % 2:
                    image[y * square : (y + 1) * square, x * square : (x + 1) * square] = 255

        corners = find_checkerboard(image, cols, rows)

        self.assertIsNotNone(corners)
        self.assertEqual(corners.shape, (cols * rows, 2))

    def test_checkerboard_metrics_include_edges_roll_and_tilt(self):
        cols = 4
        rows = 3
        points = []
        for y in range(rows):
            scale = 1.0 + 0.25 * y
            for x in range(cols):
                points.append([8 + x * 20 * scale, 5 + y * 25])
        corners = np.asarray(points, dtype=float)

        metrics = checkerboard_observation_metrics(corners, 100, 80, cols, rows, 0.15, 1.12)

        self.assertIn("left", metrics["edge_sides"])
        self.assertIn("top", metrics["edge_sides"])
        self.assertGreater(metrics["bbox_area"], 0.0)
        self.assertGreaterEqual(metrics["roll_angle_deg"], 0.0)
        self.assertTrue(metrics["tilt_sides"])

    def test_angle_bin_coverage(self):
        self.assertEqual(angle_bin_coverage([0, 10, 40, 95, 170], 6), 4)

    def test_scale_grid_coverage_splits_near_middle_far(self):
        observations = []
        for area in [0.30, 0.20, 0.10]:
            observations.append(
                {
                    "bbox_area": area,
                    "corner_points": [
                        (0.10, 0.10),
                        (0.11, 0.11),
                        (0.60, 0.10),
                        (0.61, 0.11),
                        (0.10, 0.60),
                        (0.11, 0.61),
                        (0.60, 0.60),
                        (0.61, 0.61),
                    ],
                }
            )

        summary = scale_grid_coverage(observations, min_points_per_cell=1)

        self.assertEqual([item["grid_size"] for item in summary], [2, 3, 4])
        self.assertEqual(summary[0]["band"], "near")
        self.assertEqual(summary[0]["observations"], 1)
        self.assertEqual(summary[0]["passed_cells"], 4)
        self.assertEqual(summary[0]["cell_point_comparison"], ">")
        self.assertEqual(summary[1]["band"], "middle")
        self.assertEqual(summary[2]["band"], "far")

    def test_scale_grid_coverage_uses_per_band_thresholds(self):
        observations = []
        for area in [0.30, 0.20, 0.10]:
            observations.append(
                {
                    "bbox_area": area,
                    "corner_points": [(0.10, 0.10), (0.11, 0.11)],
                }
            )

        summary = scale_grid_coverage(observations, thresholds={"near": 1, "middle": 2, "far": 1})

        self.assertEqual(summary[0]["passed_cells"], 1)
        self.assertEqual(summary[1]["passed_cells"], 0)
        self.assertEqual(summary[2]["passed_cells"], 1)

    def test_scale_grid_failure_fails_report(self):
        target_result = {
            "cameras": {
                "/cam0": {
                    "processed": 10,
                    "detected": 10,
                    "detection_ratio": 1.0,
                    "center_grid_coverage": 1.0,
                    "corner_grid_coverage": 1.0,
                    "scale_grid_coverage": [
                        {"band": "near", "passed_cells": 4, "total_cells": 4, "cell_point_threshold": 1500},
                        {"band": "middle", "passed_cells": 8, "total_cells": 9, "cell_point_threshold": 800},
                        {"band": "far", "passed_cells": 16, "total_cells": 16, "cell_point_threshold": 300},
                    ],
                }
            }
        }
        report = Report(
            "x.bag",
            ["/cam0"],
            [],
            {},
            target=target_result,
        )

        evaluate_target_result(
            report,
            args(
                min_target_observations=1,
                min_target_detection_ratio=0.2,
                min_target_center_coverage=0.35,
                min_target_corner_coverage=0.45,
                min_target_scale_grid_coverage=1.0,
            ),
            target_result,
        )

        self.assertEqual(report.status(), "FAIL")
        self.assertTrue(
            any(check.level == "FAIL" and check.name == "target_scale_grid_coverage" for check in report.checks)
        )

    def test_generate_visual_report(self):
        report = Report(
            "x.bag",
            ["/cam0"],
            [],
            {},
            checks=[],
            target={
                "cameras": {
                    "/cam0": {
                        "processed": 2,
                        "detected": 2,
                        "detection_ratio": 1.0,
                        "center_grid_coverage": 0.5,
                        "corner_grid_coverage": 0.5,
                        "edge_sides_covered": 2,
                        "roll_bins_covered": 2,
                        "tilt_sides_covered": 1,
                        "bbox_area_p90_p10_ratio": 2.0,
                        "scale_grid_coverage": [
                            {"label": "近处", "passed_cells": 4, "total_cells": 4, "coverage": 1.0, "cell_point_threshold": 1500},
                            {"label": "中处", "passed_cells": 6, "total_cells": 9, "coverage": 2.0 / 3.0, "cell_point_threshold": 800},
                            {"label": "远处", "passed_cells": 8, "total_cells": 16, "coverage": 0.5, "cell_point_threshold": 300},
                        ],
                        "_plot": {
                            "centers": [(0.25, 0.25), (0.75, 0.75)],
                            "corner_points": [(0.1, 0.1), (0.9, 0.9)],
                            "bbox_areas": [0.05, 0.1],
                            "roll_angles": [10.0, 80.0],
                        },
                    }
                }
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_visual_report(report, args(report_dir=tmpdir, target_grid=4, quiet=True))

            self.assertTrue(os.path.exists(os.path.join(tmpdir, "index.html")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "report.json")))


if __name__ == "__main__":
    unittest.main()
