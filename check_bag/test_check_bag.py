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
from check_bag.check_bag import angle_bin_coverage, checkerboard_observation_metrics, find_checkerboard, generate_visual_report


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
