#!/usr/bin/env python3
"""
convert_to_kalibr.py  (运行在 Docker 容器内, 使用原生 ROS rosbag + PyAV/cv2)

把一个 calibration.bag 转成 Kalibr 输入 bag:
  - 4 路相机 CompressedImage -> sensor_msgs/Image(mono8), topic 改名 /cam0../cam3
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

import cv2
import numpy as np
import rosbag
from sensor_msgs.msg import Image

# OpenCV 自身线程数设 1 (H.264 走 PyAV 时无并行意义; JPEG 路径逐帧解码也不需要)
cv2.setNumThreads(1)

CAM_MAP = {
    # 旧命名 (fisheye/*)
    "/fisheye/left/image_raw/compressed":   "/cam0/image_raw",
    "/fisheye/right/image_raw/compressed":  "/cam1/image_raw",
    "/fisheye/bleft/image_raw/compressed":  "/cam2/image_raw",
    "/fisheye/bright/image_raw/compressed": "/cam3/image_raw",
    # 新命名 (/camN/image/compressed) —— bag 发布者已按空间序 0 起始编号, 按序透传
    "/cam0/image/compressed": "/cam0/image_raw",
    "/cam1/image/compressed": "/cam1/image_raw",
    "/cam2/image/compressed": "/cam2/image_raw",
    "/cam3/image/compressed": "/cam3/image_raw",
}
# 容忍带/不带前导斜杠两种写法
CAM_MAP.update({k.lstrip("/"): v for k, v in list(CAM_MAP.items())})

IMU_IN_CANDIDATES = {"/imu_data_raw", "imu_data_raw", "/imu/data_raw", "imu/data_raw"}
IMU_OUT = "/imu0"


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
    实测本数据: 单线程解码, 每 packet 精确出 1 帧, 无解码延迟/重排,
    因此解出帧与当前 packet 的 header.stamp 一一对应, 无需 pending 时间戳队列。
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
        self.skipped = 0

    def decode(self, data):
        """喂入一个 packet 的字节, 返回本次解出的灰度帧列表 (通常 0 或 1 帧)。"""
        import av
        if not self._started:
            if _nal_has_keyframe(data):
                self._started = True
            else:
                self.skipped += 1
                return []
        try:
            frames = self._codec.decode(av.packet.Packet(data))
        except Exception:
            return []
        return [f.to_ndarray(format="gray") for f in frames]  # (H, W) uint8


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
            for gray in decoders[topic].decode(msg.data):
                if keep(out_topic, s):
                    wr(out_topic, header, gray)
        else:
            if keep(out_topic, s):
                wr(out_topic, header, decode_still_gray(msg.data))
        if stats["cam_in"] % 2000 == 0:
            print("  ... in=%d kept=%d imu=%d err=%d"
                  % (stats["cam_in"], stats["cam_out"], stats["imu_n"], stats["err"]))
    if is_h264:
        print("[convert] h264 skipped-before-keyframe per cam: %s"
              % {CAM_MAP[t]: d.skipped for t, d in decoders.items()})


def _convert_parallel_h264(input_path, out, cam_topics, imu_topics, min_dt, stats):
    """H.264 并行转换 (记忆验证过的架构):
      - 每路相机一个 worker 线程, 各自独立打开 bag 只读自己那一路, 单线程解码器顺序喂
        (H.264 有状态, 路内必须顺序; 路间独立可并行。PyAV decode 释放 GIL -> 近 Nx 加速)
      - IMU 单独一个 reader 线程
      - 一个单写线程从队列取出顺序写 bag (rosbag 写非线程安全)
    每 packet 精确出 1 帧, 抽帧按 header.stamp 在解码后判断。
    """
    import threading
    try:
        import queue
    except ImportError:
        import Queue as queue

    q = queue.Queue(maxsize=256)     # (out_topic_or_IMU, header_or_msg, gray_or_None)
    SENTINEL = object()

    def cam_worker(in_topic):
        out_topic = CAM_MAP[in_topic]
        dec = H264Decoder()
        b = rosbag.Bag(input_path, "r")
        last = None
        try:
            for _tp, msg, _t in b.read_messages(topics=[in_topic]):
                stats["cam_in"] += 1
                s = msg.header.stamp.to_sec()
                for gray in dec.decode(msg.data):
                    if min_dt > 0.0 and last is not None and (s - last) < min_dt:
                        continue
                    last = s
                    q.put(("CAM", out_topic, msg.header, gray))
        finally:
            b.close()
            q.put((SENTINEL, out_topic, dec.skipped, None))

    def imu_worker():
        if not imu_topics:
            q.put((SENTINEL, "imu", 0, None))
            return
        b = rosbag.Bag(input_path, "r")
        try:
            for _tp, msg, _t in b.read_messages(topics=imu_topics):
                q.put(("IMU", None, msg, None))
        finally:
            b.close()
            q.put((SENTINEL, "imu", 0, None))

    workers = [threading.Thread(target=cam_worker, args=(t,), daemon=True)
               for t in cam_topics]
    workers.append(threading.Thread(target=imu_worker, daemon=True))
    for w in workers:
        w.start()

    n_producers = len(cam_topics) + 1
    done = 0
    skipped = {}
    while done < n_producers:
        item = q.get()
        kind = item[0]
        if kind is SENTINEL:
            done += 1
            if item[1] != "imu":
                skipped[item[1]] = item[2]
            continue
        if kind == "IMU":
            msg = item[2]
            out.write(IMU_OUT, msg, msg.header.stamp)
            stats["imu_n"] += 1
        else:  # CAM
            _k, out_topic, header, gray = item
            if gray is None:
                stats["err"] += 1
            else:
                out.write(out_topic, make_mono8_msg(header, gray), header.stamp)
                stats["cam_out"] += 1
            if stats["cam_out"] % 1000 == 0 and stats["cam_out"] > 0:
                print("  ... kept=%d imu=%d err=%d"
                      % (stats["cam_out"], stats["imu_n"], stats["err"]))
    for w in workers:
        w.join()
    print("[convert] h264 skipped-before-keyframe per cam: %s" % skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cam-hz", type=float, default=30.0,
                    help="相机目标抽帧频率 (默认 30Hz; <=0 表示不降采样)")
    # --jobs/--batch 保留以兼容旧调用
    ap.add_argument("--jobs", type=int, default=8, help="(已弃用, 保留兼容)")
    ap.add_argument("--batch", type=int, default=256, help="(已弃用, 保留兼容)")
    args = ap.parse_args()

    min_dt = (1.0 / args.cam_hz) - 1e-3 if args.cam_hz > 0 else 0.0

    inbag = rosbag.Bag(args.input, "r")
    info = inbag.get_type_and_topic_info().topics
    cam_topics_present = [t for t in info if t in CAM_MAP]
    imu_topic_present = [t for t in info if t in IMU_IN_CANDIDATES]

    if not cam_topics_present:
        print("[ERROR] 输入 bag 找不到任何相机 topic; 现有: %s" % list(info))
        sys.exit(2)

    # 探测每路相机的编码格式 (取首条消息的 format 字段)。
    fmt_by_topic = {}
    for t in cam_topics_present:
        m = next(inbag.read_messages(topics=[t]))[1]
        fmt_by_topic[t] = (getattr(m, "format", "") or "").lower()

    is_h264 = any("h264" in f or "h265" in f or "hevc" in f
                  for f in fmt_by_topic.values())

    stats = {"cam_in": 0, "cam_out": 0, "imu_n": 0, "err": 0}

    print("[convert] input=%s" % args.input)
    print("[convert] cam topics: %s" % cam_topics_present)
    print("[convert] cam format : %s" % fmt_by_topic)
    print("[convert] imu topic : %s" % (imu_topic_present or "<none>"))
    print("[convert] cam target: %s" %
          ("%.1f Hz (min_dt=%.4f)" % (args.cam_hz, min_dt) if args.cam_hz > 0
           else "全帧保留 (不降采样)"))
    print("[convert] decode    : %s"
          % ("PyAV h264 (4-worker 并行)" if is_h264 else "cv2 imdecode (串行)"))

    with rosbag.Bag(args.output, "w") as out:
        if is_h264:
            _convert_parallel_h264(args.input, out, cam_topics_present,
                                   imu_topic_present, min_dt, stats)
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
