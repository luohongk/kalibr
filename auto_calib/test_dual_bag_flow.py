#!/usr/bin/env python3
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

AUTO_CALIB = Path(__file__).resolve().parent
REPO_ROOT = AUTO_CALIB.parent
KALIBR_CAMERA_CALIBRATION = (
    REPO_ROOT / "aslam_offline_calibration/kalibr/python/kalibr_camera_calibration"
)
MULTICAM_GRAPH = KALIBR_CAMERA_CALIBRATION / "MulticamGraph.py"
CAMERA_INITIALIZERS = KALIBR_CAMERA_CALIBRATION / "CameraIntializers.py"


class FakeTransformation:
    def __init__(self, matrix=None):
        self._matrix = np.eye(4) if matrix is None else np.array(matrix, dtype=float, copy=True)

    def T(self):
        return self._matrix.copy()

    def inverse(self):
        return FakeTransformation(np.linalg.inv(self._matrix))

    def __mul__(self, other):
        return FakeTransformation(np.dot(self._matrix, other._matrix))


class FakeDistortion:
    def __init__(self, parameters):
        self.parameters = np.array(parameters, dtype=float)

    def getParameters(self):
        return self.parameters

    def setParameters(self, parameters):
        self.parameters = np.array(parameters, dtype=float, copy=True)


class FakeProjection:
    def __init__(self, parameters, distortion):
        self.parameters = np.array(parameters, dtype=float)
        self._distortion = FakeDistortion(distortion)

    def getParameters(self):
        return self.parameters

    def setParameters(self, parameters):
        self.parameters = np.array(parameters, dtype=float, copy=True)

    def distortion(self):
        return self._distortion


class FakeGeometry:
    def __init__(self, parameters, distortion):
        self._projection = FakeProjection(parameters, distortion)

    def projection(self):
        return self._projection


class FakeCamera:
    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.geometry = FakeGeometry([camera_id + 1.0, camera_id + 2.0], [camera_id + 0.1])


class FakeEdge:
    def __init__(self, vertices, weight, attributes=None):
        self.tuple = tuple(vertices)
        self._attributes = {"weight": weight}
        if attributes:
            self._attributes.update(attributes)

    def __getitem__(self, key):
        return self._attributes[key]

    def __setitem__(self, key, value):
        self._attributes[key] = value


class FakeEdgeSeq:
    def __init__(self, graph):
        self.graph = graph

    def __iter__(self):
        return iter(self.graph._edges)

    def __len__(self):
        return len(self.graph._edges)

    def __getitem__(self, key):
        if isinstance(key, str):
            return [edge[key] for edge in self.graph._edges]
        return self.graph._edges[key]


class FakeVertexSeq:
    def __init__(self, graph):
        self.graph = graph

    def outdegree(self):
        degrees = [0] * self.graph.num_cameras
        for edge in self.graph._edges:
            degrees[edge.tuple[0]] += 1
            degrees[edge.tuple[1]] += 1
        return degrees


class FakeGraph:
    def __init__(self, num_cameras, edges):
        self.num_cameras = num_cameras
        self._edges = [FakeEdge((a, b), weight) for a, b, weight in edges]
        self.es = FakeEdgeSeq(self)
        self.vs = FakeVertexSeq(self)

    def adhesion(self):
        return bool(self._vertex_path(0, self.num_cameras - 1))

    def get_eid(self, source, target):
        wanted = {source, target}
        for edge_id, edge in enumerate(self._edges):
            if set(edge.tuple) == wanted:
                return edge_id
        raise ValueError("edge not found")

    def copy(self):
        copied = FakeGraph(self.num_cameras, [])
        copied._edges = [
            FakeEdge(edge.tuple, edge["weight"], dict(edge._attributes))
            for edge in self._edges
        ]
        return copied

    def delete_edges(self, edge_ids):
        removed = set(edge_ids)
        self._edges = [edge for edge_id, edge in enumerate(self._edges) if edge_id not in removed]

    def _vertex_path(self, source, target, weights=None):
        if source == target:
            return [source]
        distances = {source: 0.0}
        previous = {}
        pending = {source}
        while pending:
            vertex = min(pending, key=lambda item: distances[item])
            pending.remove(vertex)
            for edge_id, edge in enumerate(self._edges):
                if vertex not in edge.tuple:
                    continue
                neighbor = edge.tuple[1] if edge.tuple[0] == vertex else edge.tuple[0]
                cost = weights[edge_id] if weights is not None else 1.0
                distance = distances[vertex] + cost
                if distance < distances.get(neighbor, float("inf")):
                    distances[neighbor] = distance
                    previous[neighbor] = (vertex, edge_id)
                    pending.add(neighbor)
        if target not in distances:
            return []
        path = [target]
        while path[-1] != source:
            path.append(previous[path[-1]][0])
        return list(reversed(path))

    def get_shortest_paths(self, source, target=None, weights=None, output="vpath"):
        targets = range(self.num_cameras) if target is None else [target]
        paths = []
        for destination in targets:
            vertex_path = self._vertex_path(source, destination, weights)
            if output == "epath":
                edge_path = [
                    self.get_eid(vertex_path[index], vertex_path[index + 1])
                    for index in range(len(vertex_path) - 1)
                ]
                paths.append(edge_path)
            else:
                paths.append(vertex_path)
        return paths


class FakeObservationDb:
    def getAllObsTwoCams(self, cam_left, cam_right):
        return [(cam_left, cam_right)]


class MulticamHarness:
    def __init__(self, num_cameras, edges, stereo_calibrate):
        self.stereo_calls = []
        self.full_batch_calls = []
        self.cameras = [FakeCamera(camera_id) for camera_id in range(num_cameras)]

        fake_sm = types.ModuleType("sm")
        fake_sm.Transformation = FakeTransformation
        fake_sm.logDebug = lambda *_args: None
        fake_sm.logWarn = lambda *_args: None
        fake_sm.logError = lambda *_args: None

        fake_kcc = types.ModuleType("kalibr_camera_calibration")

        def record_stereo(camera_left, camera_right, observations, distortionActive=False):
            pair = (camera_left.camera_id, camera_right.camera_id)
            self.stereo_calls.append(pair)
            return stereo_calibrate(camera_left, camera_right, observations, distortionActive)

        def record_full_batch(cameras, baselines, graph):
            self.full_batch_calls.append(list(baselines))
            return True, baselines

        fake_kcc.stereoCalibrate = record_stereo
        fake_kcc.solveFullBatch = record_full_batch

        stubs = {
            "sm": fake_sm,
            "aslam_backend": types.ModuleType("aslam_backend"),
            "aslam_cv": types.ModuleType("aslam_cv"),
            "kalibr_camera_calibration": fake_kcc,
            "igraph": types.ModuleType("igraph"),
            "pylab": types.ModuleType("pylab"),
        }
        pil = types.ModuleType("PIL")
        pil.Image = types.SimpleNamespace()
        stubs["PIL"] = pil

        spec = importlib.util.spec_from_file_location("multicam_graph_under_test", MULTICAM_GRAPH)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)

        self.graph = module.MulticamCalibrationGraph.__new__(module.MulticamCalibrationGraph)
        self.graph.numCams = num_cameras
        self.graph.G = FakeGraph(num_cameras, edges)
        self.graph.obs_db = FakeObservationDb()

    def run(self):
        return self.graph.getInitialGuesses(self.cameras)


def make_transform(x_translation):
    matrix = np.eye(4)
    matrix[0, 3] = x_translation
    return FakeTransformation(matrix)


def load_camera_initializers():
    fake_sm = types.ModuleType("sm")
    fake_sm.Transformation = FakeTransformation
    fake_sm.RotationVector = type("RotationVector", (), {})
    fake_sm.logDebug = lambda *_args: None
    fake_sm.logWarn = lambda *_args: None
    fake_sm.logError = lambda *_args: None
    fake_sm.getLoggingLevel = lambda: None
    fake_sm.LoggingLevel = types.SimpleNamespace(Debug="debug")

    stubs = {
        "sm": fake_sm,
        "aslam_backend": types.ModuleType("aslam_backend"),
        "aslam_cv": types.ModuleType("aslam_cv"),
    }
    spec = importlib.util.spec_from_file_location(
        "camera_initializers_under_test", CAMERA_INITIALIZERS
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class FakePnPGeometry:
    def estimateTransformation(self, observation):
        if isinstance(observation, Exception):
            raise observation
        return observation


class FakePnPCamera:
    def __init__(self):
        self.geometry = FakePnPGeometry()


def pnp_result(success, translation, finite=True):
    transform = make_transform(translation)
    if not finite:
        transform._matrix[0, 0] = np.nan
    return success, transform


def load_converter():
    cv2 = types.ModuleType("cv2")
    cv2.setNumThreads = lambda _n: None
    cv2.IMREAD_GRAYSCALE = 0
    cv2.imdecode = lambda *_args: None
    sys.modules["cv2"] = cv2
    sys.modules["rosbag"] = types.ModuleType("rosbag")

    sensor_msgs = types.ModuleType("sensor_msgs")
    sensor_msgs_msg = types.ModuleType("sensor_msgs.msg")
    sensor_msgs_msg.Image = type("Image", (), {})
    sensor_msgs.msg = sensor_msgs_msg
    sys.modules["sensor_msgs"] = sensor_msgs
    sys.modules["sensor_msgs.msg"] = sensor_msgs_msg

    spec = importlib.util.spec_from_file_location(
        "convert_to_kalibr", AUTO_CALIB / "convert_to_kalibr.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CameraSelectionTest(unittest.TestCase):
    def test_selects_only_requested_camera(self):
        converter = load_converter()
        present = [
            "/cam0/image/compressed",
            "/cam1/image/compressed",
            "/cam2/image/compressed",
            "/cam3/image/compressed",
        ]
        self.assertEqual(
            converter.select_camera_topics(present, "0"),
            ["/cam0/image/compressed"],
        )

    def test_rejects_mixed_valid_and_invalid_camera_indices(self):
        converter = load_converter()
        with self.assertRaises(ValueError):
            converter.select_camera_topics(["/cam0/image/compressed"], "0,4")

    def test_h264_decoder_defers_gray_conversion_until_after_sampling(self):
        converter = load_converter()

        class FakeCodec:
            thread_count = 0

            def decode(self, _packet):
                return [fake_frame]

        class FakeCodecContext:
            @staticmethod
            def create(_codec, _mode):
                return FakeCodec()

        class FakePacket:
            def __init__(self, data):
                self.data = data

        class FakeFrame:
            def __init__(self):
                self.gray_conversions = 0

            def to_ndarray(self, format):
                self.gray_conversions += 1
                self.assert_format = format
                return np.zeros((2, 2), dtype=np.uint8)

        fake_frame = FakeFrame()
        fake_av = types.ModuleType("av")
        fake_av.CodecContext = FakeCodecContext
        fake_av.packet = types.SimpleNamespace(Packet=FakePacket)
        sys.modules["av"] = fake_av

        decoder = converter.H264Decoder()
        outputs = decoder.decode(b"\x00\x00\x00\x01\x67", object())

        self.assertEqual(len(outputs), 1)
        self.assertIs(outputs[0][1], fake_frame)
        self.assertEqual(fake_frame.gray_conversions, 0)

    def test_converter_sampling_interval_matches_kalibr(self):
        source = (AUTO_CALIB / "convert_to_kalibr.py").read_text()
        self.assertIn("min_dt = (1.0 / args.cam_hz)", source)
        self.assertNotIn("(1.0 / args.cam_hz) - 1e-3", source)


class MulticamGraphBaselineSelectionTest(unittest.TestCase):
    def test_retries_fallback_edge_and_restores_failed_camera_parameters(self):
        entry_parameters = {}

        def stereo(camera_left, camera_right, _observations, _distortion_active):
            pair = (camera_left.camera_id, camera_right.camera_id)
            entry_parameters[pair] = (
                camera_left.geometry.projection().getParameters().copy(),
                camera_right.geometry.projection().getParameters().copy(),
            )
            if pair == (0, 1):
                camera_left.geometry.projection().parameters[:] = np.nan
                camera_left.geometry.projection().distortion().parameters[:] = np.nan
                camera_right.geometry.projection().parameters[:] = np.nan
                return False, make_transform(100)
            camera_left.geometry.projection().parameters += 0.01
            camera_right.geometry.projection().parameters += 0.01
            return True, make_transform(10 * camera_left.camera_id + camera_right.camera_id)

        harness = MulticamHarness(
            4,
            [(0, 1, 100), (1, 2, 90), (0, 2, 80), (2, 3, 70)],
            stereo,
        )
        original_cam0 = harness.cameras[0].geometry.projection().getParameters().copy()
        original_cam1 = harness.cameras[1].geometry.projection().getParameters().copy()
        original_distortion0 = (
            harness.cameras[0].geometry.projection().distortion().getParameters().copy()
        )

        baselines = harness.run()

        self.assertEqual(harness.stereo_calls, [(0, 1), (1, 2), (0, 2), (2, 3)])
        np.testing.assert_array_equal(entry_parameters[(1, 2)][0], original_cam1)
        np.testing.assert_array_equal(entry_parameters[(0, 2)][0], original_cam0)
        np.testing.assert_array_equal(
            harness.cameras[0].geometry.projection().distortion().getParameters(),
            original_distortion0,
        )
        self.assertEqual(len(harness.graph.optimal_baseline_edges), 3)
        self.assertEqual(len(baselines), 3)
        np.testing.assert_allclose(
            [baseline.T()[0, 3] for baseline in baselines],
            [-10.0, 12.0, 23.0],
        )
        np.testing.assert_allclose(
            harness.cameras[0].geometry.projection().getParameters(),
            original_cam0 + 0.01,
        )
        self.assertEqual(len(harness.full_batch_calls), 1)

    def test_orders_candidates_stably_and_skips_cycle_edges(self):
        def stereo(camera_left, camera_right, _observations, _distortion_active):
            return True, make_transform(10 * camera_left.camera_id + camera_right.camera_id)

        harness = MulticamHarness(
            4,
            [(2, 1, 90), (1, 3, 80), (2, 0, 100), (1, 0, 100)],
            stereo,
        )

        harness.run()

        self.assertEqual(harness.stereo_calls, [(0, 1), (0, 2), (1, 3)])

    def test_rejects_invalid_attempts_and_tries_fallback_edges(self):
        invalid_cases = {
            "false result": lambda left, _right: (False, make_transform(1)),
            "non-finite baseline": lambda left, _right: (
                True,
                FakeTransformation(np.full((4, 4), np.nan)),
            ),
            "non-finite projection": lambda left, _right: (
                left.geometry.projection().parameters.__setitem__(0, np.inf)
                or (True, make_transform(1))
            ),
            "non-finite distortion": lambda left, _right: (
                left.geometry.projection().distortion().parameters.__setitem__(0, np.nan)
                or (True, make_transform(1))
            ),
        }

        for case_name, invalid_result in invalid_cases.items():
            with self.subTest(case_name=case_name):
                def stereo(camera_left, camera_right, _observations, _distortion_active):
                    if (camera_left.camera_id, camera_right.camera_id) == (0, 1):
                        return invalid_result(camera_left, camera_right)
                    return True, make_transform(10 * camera_left.camera_id + camera_right.camera_id)

                harness = MulticamHarness(
                    3,
                    [(0, 1, 100), (0, 2, 90), (1, 2, 80)],
                    stereo,
                )
                original_projection = (
                    harness.cameras[0].geometry.projection().getParameters().copy()
                )
                original_distortion = (
                    harness.cameras[0].geometry.projection().distortion().getParameters().copy()
                )

                harness.run()

                self.assertEqual(harness.stereo_calls, [(0, 1), (0, 2), (1, 2)])
                np.testing.assert_array_equal(
                    harness.cameras[0].geometry.projection().getParameters(),
                    original_projection,
                )
                np.testing.assert_array_equal(
                    harness.cameras[0].geometry.projection().distortion().getParameters(),
                    original_distortion,
                )

    def test_restores_parameters_when_stereo_calibration_raises(self):
        def stereo(camera_left, camera_right, _observations, _distortion_active):
            if (camera_left.camera_id, camera_right.camera_id) == (0, 1):
                camera_left.geometry.projection().parameters[:] = 999
                raise RuntimeError("optimizer exploded")
            return True, make_transform(10 * camera_left.camera_id + camera_right.camera_id)

        harness = MulticamHarness(
            3,
            [(0, 1, 100), (0, 2, 90), (1, 2, 80)],
            stereo,
        )
        original_projection = harness.cameras[0].geometry.projection().getParameters().copy()

        harness.run()

        np.testing.assert_array_equal(
            harness.cameras[0].geometry.projection().getParameters(),
            original_projection,
        )

    def test_reports_failures_and_components_when_no_tree_can_be_built(self):
        def stereo(camera_left, camera_right, _observations, _distortion_active):
            pair = (camera_left.camera_id, camera_right.camera_id)
            if pair == (0, 1):
                camera_left.geometry.projection().parameters += 0.5
                camera_right.geometry.projection().parameters += 0.5
                return True, make_transform(1)
            if pair == (1, 2):
                camera_left.geometry.projection().parameters[:] = np.nan
            return False, make_transform(2)

        harness = MulticamHarness(
            3,
            [(0, 1, 100), (1, 2, 90), (0, 2, 80)],
            stereo,
        )
        cam1_before_success = harness.cameras[1].geometry.projection().getParameters().copy()

        with self.assertRaisesRegex(RuntimeError, r"cam1-cam2.*components.*\[0, 1\].*\[2\]"):
            harness.run()

        np.testing.assert_array_equal(
            harness.cameras[1].geometry.projection().getParameters(),
            cam1_before_success + 0.5,
        )
        self.assertEqual(harness.full_batch_calls, [])


class StereoInitializationTest(unittest.TestCase):
    def test_jointly_optimizes_intrinsics_and_baseline(self):
        source = CAMERA_INITIALIZERS.read_text()
        self.assertEqual(
            source.count("setDvActiveStatus(True, distortionActive, False)"),
            2,
        )

    def test_baseline_initialization_uses_only_two_sided_valid_finite_pnp(self):
        initializers = load_camera_initializers()
        camera_left = FakePnPCamera()
        camera_right = FakePnPCamera()
        observations = [
            (pnp_result(True, 1), pnp_result(True, 4)),
            (pnp_result(False, 10), pnp_result(True, 20)),
            (pnp_result(True, 30), pnp_result(False, 40)),
            (pnp_result(True, 50, finite=False), pnp_result(True, 60)),
            (pnp_result(True, 70), pnp_result(True, 80, finite=False)),
        ]

        baselines = initializers._validStereoBaselineTransforms(
            camera_left, camera_right, observations
        )

        self.assertEqual(len(baselines), 1)
        self.assertAlmostEqual(baselines[0].T()[0, 3], -3.0)

    def test_target_pose_falls_back_to_valid_right_camera_pnp(self):
        initializers = load_camera_initializers()
        camera_left = FakePnPCamera()
        camera_right = FakePnPCamera()
        baseline = make_transform(5)

        pose = initializers._stereoTargetPoseGuess(
            camera_left,
            camera_right,
            pnp_result(False, 1),
            pnp_result(True, 7),
            baseline,
        )

        self.assertIsNotNone(pose)
        self.assertAlmostEqual(pose.T()[0, 3], 12.0)

    def test_target_pose_rejects_failed_and_non_finite_pnp(self):
        initializers = load_camera_initializers()
        camera_left = FakePnPCamera()
        camera_right = FakePnPCamera()

        pose = initializers._stereoTargetPoseGuess(
            camera_left,
            camera_right,
            pnp_result(False, 1),
            pnp_result(True, 2, finite=False),
            make_transform(5),
        )

        self.assertIsNone(pose)

    def test_pnp_exception_invalidates_only_that_observation(self):
        initializers = load_camera_initializers()
        camera_left = FakePnPCamera()
        camera_right = FakePnPCamera()
        observations = [
            (RuntimeError("bad PnP"), pnp_result(True, 4)),
            (pnp_result(True, 1), pnp_result(True, 4)),
        ]

        baselines = initializers._validStereoBaselineTransforms(
            camera_left, camera_right, observations
        )

        self.assertEqual(len(baselines), 1)
        self.assertAlmostEqual(baselines[0].T()[0, 3], -3.0)

    def test_target_pose_uses_non_commutative_right_to_left_composition(self):
        initializers = load_camera_initializers()
        camera_left = FakePnPCamera()
        camera_right = FakePnPCamera()
        right_pose_matrix = np.eye(4)
        right_pose_matrix[:2, :2] = [[0, -1], [1, 0]]
        right_pose_matrix[0, 3] = 2
        baseline_matrix = np.eye(4)
        baseline_matrix[1, 3] = 3

        pose = initializers._stereoTargetPoseGuess(
            camera_left,
            camera_right,
            pnp_result(False, 0),
            (True, FakeTransformation(right_pose_matrix)),
            FakeTransformation(baseline_matrix),
        )

        np.testing.assert_allclose(
            pose.T(),
            np.dot(right_pose_matrix, baseline_matrix),
        )

    def test_stereo_calibration_rejects_non_finite_supplied_baseline(self):
        initializers = load_camera_initializers()
        invalid_baseline = FakeTransformation(np.full((4, 4), np.nan))

        success, _baseline = initializers.stereoCalibrate(
            FakePnPCamera(), FakePnPCamera(), [], baseline=invalid_baseline
        )

        self.assertFalse(success)


class DualBagShellFlowTest(unittest.TestCase):
    def test_run_one_uses_separate_bags_for_camera_and_imu_camera(self):
        script = (AUTO_CALIB / "run_one.sh").read_text()
        self.assertIn('CAL_4CAM_BAG="$DATA_FOLDER/calibration_4cam.bag"', script)
        self.assertIn('CAL_IMUCAM_BAG="$DATA_FOLDER/calibration_cam0_imu.bag"', script)
        self.assertIn('--input "$CAL_4CAM_BAG" --output "$CAM_CONV_BAG"', script)
        self.assertIn('--input "$CAL_IMUCAM_BAG" --output "$IMUCAM_CONV_BAG"', script)
        self.assertIn('--camera-indices 0', script)
        self.assertIn('--cam-hz "$CAM_CONVERT_HZ" --no-imu', script)
        self.assertIn('--cam-hz "$IMUCAM_CONVERT_HZ"', script)
        self.assertIn('--bag "$CAM_CONV_BAG"', script)
        self.assertIn('--bag "$IMUCAM_CONV_BAG"', script)

    def test_run_one_stops_when_either_kalibr_command_fails(self):
        script = (AUTO_CALIB / "run_one.sh").read_text()
        self.assertIn('if ! ( cd "$OUTDIR" && \\\n  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_cameras', script)
        self.assertIn('if ! ( cd "$OUTDIR" && \\\n  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_imu_camera', script)

    def test_run_one_locks_dataset_work_directory(self):
        script = (AUTO_CALIB / "run_one.sh").read_text()
        self.assertIn('LOCK_FILE="/tmp/auto_calib_${LOCK_KEY}.lock"', script)
        self.assertIn('if ! flock -n 9; then', script)
        self.assertLess(script.index('if ! flock -n 9; then'), script.index('rm -rf "$WORK"'))

    def test_run_one_enables_two_stage_intrinsics_freeze(self):
        script = (AUTO_CALIB / "run_one.sh").read_text()
        self.assertIn('CAM_FREEZE_INTRINSICS_RMSE="${CAM_FREEZE_INTRINSICS_RMSE:-0.2}"', script)
        self.assertIn('CAM_NO_SHUFFLE="${CAM_NO_SHUFFLE:-0}"', script)
        self.assertIn('--freeze-intrinsics-rmse "$CAM_FREEZE_INTRINSICS_RMSE"', script)
        self.assertIn('CAM_CALIB_EXTRA_ARGS+=(--use-blakezisserman)', script)
        self.assertIn('CAM_CALIB_EXTRA_ARGS+=(--no-shuffle)', script)

    def test_run_all_discovers_four_camera_bag(self):
        script = (AUTO_CALIB / "run_all.sh").read_text()
        self.assertIn('calibration_4cam.bag', script)
        self.assertIn('calibration_cam0_imu.bag', script)

    def test_run_all_syncs_intrinsics_freeze_sources(self):
        script = (AUTO_CALIB / "run_all.sh").read_text()
        self.assertIn('kalibr_calibrate_cameras"', script)
        self.assertIn('CameraCalibrator.py"', script)
        self.assertIn('CameraUtils.py"', script)
        self.assertIn('TargetExtractor.py"', script)

    def test_corner_extraction_caps_default_worker_count(self):
        source = (REPO_ROOT / "aslam_offline_calibration/kalibr/python/kalibr_common/TargetExtractor.py").read_text()
        self.assertIn("min(16, max(1, multiprocessing.cpu_count()-1))", source)
        self.assertIn("KALIBR_EXTRACT_JOBS", source)


if __name__ == "__main__":
    unittest.main()
