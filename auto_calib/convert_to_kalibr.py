#!/usr/bin/env python3
"""
convert_to_kalibr.py  (运行在 Docker 容器内, 使用原生 ROS rosbag + PyAV/cv2)

把一个 calibration bag 转成 Kalibr 输入 bag:
  - 指定相机的 CompressedImage -> sensor_msgs/Image(mono8), topic 改名 /cam0../cam3
    * 支持 JPEG/PNG (format=jpeg/png): 每帧独立用 cv2.imdecode 解码
    * 支持 H.264 (format=h264): 帧间压缩流, 每路相机各维护一个 PyAV 解码器,
      按 bag 时间顺序顺序喂 packet 取 frame (不能并行/乱序)
  - 相机降采样到约 --cam-hz (默认 30Hz, <=0 不降采样), 每路相机按 header 时间戳独立抽帧
  - IMU 原样透传, topic 改名 /imu0 (保留全频率, 用于 cam-imu 标定)

用法:
  python3 convert_to_kalibr.py --input calibration.bag --output kalibr_input.bag --cam-hz 30
  python3 convert_to_kalibr.py --input ... --output ... --cam-hz 0
"""
import argparse
import sys
import time

import cv2
import numpy as np
import rosbag
from sensor_msgs.msg import Image

# OpenCV 自身线程数设 1 (H.264 走 PyAV 时无并行意义; JPEG 路径逐帧解码也不需要)
cv2.setNumThreads(1)

CAM_MAP = {
    # 旧命名 (fisheye/*): 必须按物理空间从左到右排序 bleft->left->right->bright,
    # 这样相邻对 (0,1)(1,2)(2,3) 才都是物理相邻、共视最好, 立体标定才能收敛。
    "/fisheye/bleft/image_raw/compressed":  "/cam0/image_raw",
    "/fisheye/left/image_raw/compressed":   "/cam1/image_raw",
    "/fisheye/right/image_raw/compressed":  "/cam2/image_raw",
    "/fisheye/bright/image_raw/compressed": "/cam3/image_raw",
    # 新设备统一约定: cam0=bleft, cam1=left, cam2=right, cam3=bright.
    "/cam0/image/compressed": "/cam0/image_raw",
    "/cam1/image/compressed": "/cam1/image_raw",
    "/cam2/image/compressed": "/cam2/image_raw",
    "/cam3/image/compressed": "/cam3/image_raw",
}
# 容忍带/不带前导斜杠两种写法
CAM_MAP.update({k.lstrip("/"): v for k, v in list(CAM_MAP.items())})

IMU_IN_CANDIDATES = {"/imu_data_raw", "imu_data_raw", "/imu/data_raw", "imu/data_raw"}
IMU_OUT = "/imu0"


def select_camera_topics(present_topics, camera_indices):
    """按输出 cam 编号筛选输入话题；camera_indices 为 all 或逗号分隔编号。"""
    if camera_indices == "all":
        return list(present_topics)
    try:
        selected = {int(value.strip()) for value in camera_indices.split(",")}
    except ValueError:
        raise ValueError("--camera-indices 必须是 all 或逗号分隔的整数，例如 0 或 0,1,2,3")
    invalid = selected - {0, 1, 2, 3}
    if not selected or invalid:
        raise ValueError("--camera-indices 仅支持 0,1,2,3，非法编号: %s"
                         % sorted(invalid))
    wanted_outputs = {"/cam%d/image_raw" % index for index in selected}
    return [topic for topic in present_topics if CAM_MAP[topic] in wanted_outputs]


def make_mono8_msg(header, gray):
    h, w = gray.shape
    msg = Image()
    msg.header = header
    msg.height = h
    msg.width = w
    msg.encoding = "mono8"
    msg.is_bigendian = 0
    msg.step = w
    msg.data = gray.tobytes()
    return msg


def decode_still_gray(data):
    """JPEG/PNG 等静态图: 每帧独立解码为灰度 ndarray, 失败返回 None。"""
    buf = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)


def _nal_has_keyframe(data):
    """扫描 annexb 起始码, 判断该 packet 是否含 SPS(7)/IDR(5) —— 即可作为解码起点。
    本数据实测: bag 常从流中途(纯 P 帧)开始录, 开头若干包无 SPS/PPS, 必须跳过
    直到第一个关键帧, 否则 PyAV 报 'non-existing PPS 0 referenced' 解码失败。
    """
    d = bytes(data)
    i, n = 0, len(d)
    while i < n - 4:
        if d[i] == 0 and d[i + 1] == 0 and d[i + 2] == 0 and d[i + 3] == 1:
            if (d[i + 4] & 0x1f) in (5, 7):
                return True
            i += 4
        elif d[i] == 0 and d[i + 1] == 0 and d[i + 2] == 1:
            if (d[i + 3] & 0x1f) in (5, 7):
                return True
            i += 3
        else:
            i += 1
    return False


class H264Decoder:
    """H.264 帧间压缩流: 每路相机维护一个持续的 PyAV 解码器, 顺序喂 packet。
    解码器可能延迟吐帧, 因此用 pending header 队列把输出帧按顺序配回
    原始 packet 的 header.stamp, 避免把延迟帧写成当前 packet 的时间戳。
    起始要求: 必须从第一个关键帧(含 SPS/IDR)开始喂, 之前的纯 P 帧丢弃。
    解码器有状态, 单路顺序喂, 不能并行/乱序; thread_count=1 (记忆: FRAME 模式反而慢数倍)。
    """

    def __init__(self):
        import av  # 延迟导入: 只有真遇到 h264 才要求 PyAV 存在
        self._codec = av.CodecContext.create("h264", "r")
        try:
            self._codec.thread_count = 1
        except Exception:
            pass
        self._started = False
        self._pending_headers = []
        self.skipped = 0

    def decode(self, data, header):
        """喂入一个 packet 的字节, 返回 [(header, av_frame), ...]。

        延迟 ndarray/灰度转换到抽帧判断之后。H.264 预测链仍然完整解码，
        但不再为最终会丢弃的帧执行昂贵的像素格式转换。
        """
        import av
        if not self._started:
            if _nal_has_keyframe(data):
                self._started = True
            else:
                self.skipped += 1
                return []
        self._pending_headers.append(header)
        try:
            frames = self._codec.decode(av.packet.Packet(data))
        except Exception:
            if self._pending_headers:
                self._pending_headers.pop()
            return []
        decoded = []
        for frame in frames:
            frame_header = self._pending_headers.pop(0) if self._pending_headers else header
            decoded.append((frame_header, frame))
        return decoded


def _convert_serial(inbag, out, cam_topics, imu_topics, min_dt, is_h264, stats):
    """单线程转换 (非 h264, 或作为回退)。"""
    decoders = {t: H264Decoder() for t in cam_topics} if is_h264 else {}
    last_kept = {}

    def keep(out_topic, s):
        if min_dt <= 0.0:
            return True
        prev = last_kept.get(out_topic)
        if prev is not None and (s - prev) < min_dt:
            return False
        last_kept[out_topic] = s
        return True

    def wr(out_topic, header, gray):
        if gray is None:
            stats["err"] += 1
            return
        out.write(out_topic, make_mono8_msg(header, gray), header.stamp)
        stats["cam_out"] += 1

    for topic, msg, t in inbag.read_messages(topics=cam_topics + imu_topics):
        if topic in IMU_IN_CANDIDATES:
            out.write(IMU_OUT, msg, msg.header.stamp)
            stats["imu_n"] += 1
            continue
        stats["cam_in"] += 1
        out_topic = CAM_MAP[topic]
        header = msg.header
        s = header.stamp.to_sec()
        if is_h264:
            for frame_header, frame in decoders[topic].decode(msg.data, header):
                if keep(out_topic, frame_header.stamp.to_sec()):
                    gray = frame.to_ndarray(format="gray")
                    wr(out_topic, frame_header, gray)
        else:
            if keep(out_topic, s):
                wr(out_topic, header, decode_still_gray(msg.data))
        if stats["cam_in"] % 2000 == 0:
            print("  ... in=%d kept=%d imu=%d err=%d"
                  % (stats["cam_in"], stats["cam_out"], stats["imu_n"], stats["err"]))
    if is_h264:
        print("[convert] h264 skipped-before-keyframe per cam: %s"
              % {CAM_MAP[t]: d.skipped for t, d in decoders.items()})


def _convert_parallel_h264(inbag, out, cam_topics, imu_topics, min_dt, stats,
                           total_cam_inputs=None):
    """单次读 bag、四路 H.264 并行解码、单线程写 bag。

    旧实现让每个相机和 IMU 各自打开并完整扫描一次大 bag，FUSE/磁盘需要
    重复读取约五遍。这里由主线程只顺序扫描一次，再把相机 packet 分发到
    四个有状态解码器。H.264 预测链不能跳 packet，但抽帧淘汰的 decoded
    frame 不做 gray ndarray 转换、也不写输出 bag。
    """
    import threading
    try:
        import queue
    except ImportError:
        import Queue as queue

    input_queues = {topic: queue.Queue(maxsize=64) for topic in cam_topics}
    output_queue = queue.Queue(maxsize=128)
    INPUT_DONE = object()
    OUTPUT_DONE = object()

    def cam_worker(in_topic):
        out_topic = CAM_MAP[in_topic]
        dec = H264Decoder()
        last = None
        input_queue = input_queues[in_topic]
        while True:
            msg = input_queue.get()
            if msg is INPUT_DONE:
                break
            for frame_header, frame in dec.decode(msg.data, msg.header):
                frame_s = frame_header.stamp.to_sec()
                if min_dt > 0.0 and last is not None and (frame_s - last) < min_dt:
                    continue
                last = frame_s
                gray = frame.to_ndarray(format="gray")
                output_queue.put(("CAM", out_topic, frame_header, gray))
        output_queue.put(("CAM_DONE", out_topic, dec.skipped, None))

    def writer_worker():
        while True:
            item = output_queue.get()
            kind = item[0]
            if kind is OUTPUT_DONE:
                return
            if kind == "IMU":
                msg = item[2]
                out.write(IMU_OUT, msg, msg.header.stamp)
                stats["imu_n"] += 1
            elif kind == "CAM":
                _k, out_topic, header, gray = item
                if gray is None:
                    stats["err"] += 1
                else:
                    out.write(out_topic, make_mono8_msg(header, gray), header.stamp)
                    stats["cam_out"] += 1
                    if stats["cam_out"] % 1000 == 0:
                        print("  ... kept=%d input=%d imu=%d err=%d"
                              % (stats["cam_out"], stats["cam_in"],
                                 stats["imu_n"], stats["err"]))
            else:  # CAM_DONE
                skipped[item[1]] = item[2]

    workers = [threading.Thread(target=cam_worker, args=(t,), daemon=True)
               for t in cam_topics]
    skipped = {}
    writer = threading.Thread(target=writer_worker, daemon=True)
    writer.start()
    for w in workers:
        w.start()

    read_topics = list(cam_topics) + list(imu_topics)
    started_at = time.monotonic()
    next_progress = 2000
    for topic, msg, _t in inbag.read_messages(topics=read_topics):
        if topic in input_queues:
            stats["cam_in"] += 1
            input_queues[topic].put(msg)
            if stats["cam_in"] >= next_progress:
                elapsed = max(time.monotonic() - started_at, 1e-6)
                rate = stats["cam_in"] / elapsed
                if total_cam_inputs:
                    percent = 100.0 * stats["cam_in"] / total_cam_inputs
                    remaining = max(total_cam_inputs - stats["cam_in"], 0)
                    eta = remaining / rate if rate > 0 else 0.0
                    print("  ... input=%d/%d (%.1f%%) kept=%d rate=%.0f pkt/s ETA=%.0fs imu=%d err=%d"
                          % (stats["cam_in"], total_cam_inputs, percent,
                             stats["cam_out"], rate, eta,
                             stats["imu_n"], stats["err"]))
                else:
                    print("  ... input=%d kept=%d rate=%.0f pkt/s imu=%d err=%d"
                          % (stats["cam_in"], stats["cam_out"], rate,
                             stats["imu_n"], stats["err"]))
                next_progress += 2000
        else:
            output_queue.put(("IMU", None, msg, None))

    for input_queue in input_queues.values():
        input_queue.put(INPUT_DONE)
    for w in workers:
        w.join()
    output_queue.put((OUTPUT_DONE, None, None, None))
    writer.join()
    print("[convert] h264 skipped-before-keyframe per cam: %s" % skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cam-hz", type=float, default=30.0,
                    help="相机目标抽帧频率 (默认 30Hz; <=0 表示不降采样)")
    ap.add_argument("--camera-indices", default="all",
                    help="要转换的相机编号: all(默认) 或逗号分隔编号，例如 0")
    ap.add_argument("--no-imu", action="store_true",
                    help="不复制 IMU topic（纯相机标定 bag 可减少扫描和输出）")
    # --jobs/--batch 保留以兼容旧调用
    ap.add_argument("--jobs", type=int, default=8, help="(已弃用, 保留兼容)")
    ap.add_argument("--batch", type=int, default=256, help="(已弃用, 保留兼容)")
    args = ap.parse_args()

    # Match Kalibr BagImageDatasetReader.truncateIndicesFromFreq exactly.
    # Consequently passing the same --bag-freq downstream is idempotent and
    # cannot accidentally halve the already sampled stream.
    min_dt = (1.0 / args.cam_hz) if args.cam_hz > 0 else 0.0

    inbag = rosbag.Bag(args.input, "r")
    info = inbag.get_type_and_topic_info().topics
    all_cam_topics = [t for t in info if t in CAM_MAP]
    try:
        cam_topics_present = select_camera_topics(all_cam_topics, args.camera_indices)
    except ValueError as exc:
        print("[ERROR] %s" % exc)
        sys.exit(2)
    imu_topic_present = ([] if args.no_imu else
                         [t for t in info if t in IMU_IN_CANDIDATES])

    if not cam_topics_present:
        print("[ERROR] 输入 bag 找不到指定相机 topic (camera-indices=%s); 现有: %s"
              % (args.camera_indices, list(info)))
        sys.exit(2)

    # 探测每路相机的编码格式 (取首条消息的 format 字段)。
    fmt_by_topic = {}
    for t in cam_topics_present:
        m = next(inbag.read_messages(topics=[t]))[1]
        fmt_by_topic[t] = (getattr(m, "format", "") or "").lower()

    is_h264 = any("h264" in f or "h265" in f or "hevc" in f
                  for f in fmt_by_topic.values())

    stats = {"cam_in": 0, "cam_out": 0, "imu_n": 0, "err": 0}
    total_cam_inputs = sum(info[t].message_count for t in cam_topics_present)

    print("[convert] input=%s" % args.input)
    print("[convert] cam topics: %s" % cam_topics_present)
    print("[convert] cam format : %s" % fmt_by_topic)
    print("[convert] imu topic : %s" % (imu_topic_present or "<none>"))
    print("[convert] cam target: %s" %
          ("%.1f Hz (min_dt=%.4f)" % (args.cam_hz, min_dt) if args.cam_hz > 0
           else "全帧保留 (不降采样)"))
    print("[convert] decode    : %s"
          % ("PyAV h264 (单次读bag + %d路并行解码; 仅保留帧转灰度)"
             % len(cam_topics_present) if is_h264 else "cv2 imdecode (串行)"))

    with rosbag.Bag(args.output, "w") as out:
        if is_h264:
            _convert_parallel_h264(inbag, out, cam_topics_present,
                                   imu_topic_present, min_dt, stats,
                                   total_cam_inputs=total_cam_inputs)
        else:
            _convert_serial(inbag, out, cam_topics_present,
                            imu_topic_present, min_dt, is_h264, stats)

    inbag.close()
    print("[convert] done: cam_in=%d cam_out=%d imu=%d err=%d" %
          (stats["cam_in"], stats["cam_out"], stats["imu_n"], stats["err"]))
    if stats["cam_out"] == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
