#!/usr/bin/env python3
"""
convert_to_kalibr.py  (运行在 Docker 容器内, 使用原生 ROS rosbag + cv2)

把一个 calibration.bag 转成 Kalibr 输入 bag:
  - 4 路 CompressedImage(JPEG) -> sensor_msgs/Image(mono8), topic 改名 /cam0../cam3
  - 相机降采样到约 --cam-hz (默认 30Hz, <=0 不降采样), 每路相机按 header 时间戳独立抽帧
  - IMU 原样透传, topic 改名 /imu0 (保留全频率, 用于 cam-imu 标定)
  - JPEG 解码用线程池并行 (--jobs): cv2.imdecode 释放 GIL, 多线程可有效加速。

用法:
  python3 convert_to_kalibr.py --input calibration.bag --output kalibr_input.bag --cam-hz 30
  python3 convert_to_kalibr.py --input ... --output ... --cam-hz 0 --jobs 8
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import rosbag
from sensor_msgs.msg import Image

# OpenCV 自身线程数设 1, 把并行交给我们的线程池, 避免线程过度订阅互相拖累
cv2.setNumThreads(1)

CAM_MAP = {
    "/fisheye/left/image_raw/compressed":   "/cam0/image_raw",
    "/fisheye/right/image_raw/compressed":  "/cam1/image_raw",
    "/fisheye/bleft/image_raw/compressed":  "/cam2/image_raw",
    "/fisheye/bright/image_raw/compressed": "/cam3/image_raw",
}
# 容忍带/不带前导斜杠两种写法
CAM_MAP.update({k.lstrip("/"): v for k, v in list(CAM_MAP.items())})

IMU_IN_CANDIDATES = {"/imu_data_raw", "imu_data_raw"}
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


def decode_gray(data):
    buf = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cam-hz", type=float, default=30.0,
                    help="相机目标抽帧频率 (默认 30Hz; <=0 表示不降采样)")
    ap.add_argument("--jobs", type=int, default=0,
                    help="JPEG 解码线程数 (默认 0 = 自动取 CPU 核数)")
    ap.add_argument("--batch", type=int, default=256,
                    help="并行解码批大小 (越大并行度越高, 内存占用越多)")
    args = ap.parse_args()

    # 默认线程数: 取 CPU 核数但封顶 32。核数过多(如 384)时线程过度订阅
    # 反而因内存带宽/调度开销变慢, 且每帧解码后的 mono8 占内存。
    jobs = args.jobs if args.jobs > 0 else min(os.cpu_count() or 4, 32)
    min_dt = (1.0 / args.cam_hz) - 1e-3 if args.cam_hz > 0 else 0.0

    inbag = rosbag.Bag(args.input, "r")
    info_topics = inbag.get_type_and_topic_info().topics
    cam_topics_present = [t for t in info_topics if t in CAM_MAP]
    imu_topic_present = [t for t in info_topics if t in IMU_IN_CANDIDATES]

    if not cam_topics_present:
        print("[ERROR] 输入 bag 找不到任何相机 topic; 现有: %s" % list(info_topics))
        sys.exit(2)

    read_topics = cam_topics_present + imu_topic_present

    last_kept = {}          # out_topic -> last kept stamp (sec)
    stats = {"cam_in": 0, "cam_out": 0, "imu_n": 0, "err": 0}

    print("[convert] input=%s" % args.input)
    print("[convert] cam topics: %s" % cam_topics_present)
    print("[convert] imu topic : %s" % (imu_topic_present or "<none>"))
    print("[convert] cam target: %s" %
          ("%.1f Hz (min_dt=%.4f)" % (args.cam_hz, min_dt) if args.cam_hz > 0
           else "全帧保留 (不降采样)"))
    print("[convert] decode jobs=%d batch=%d" % (jobs, args.batch))

    def flush(out, pool, batch):
        # batch: [(out_topic, header, jpeg_bytes, t), ...] -> 并行解码后顺序写出
        if not batch:
            return
        grays = pool.map(decode_gray, [b[2] for b in batch])
        for (out_topic, header, _d, t), gray in zip(batch, grays):
            if gray is None:
                stats["err"] += 1
                continue
            out.write(out_topic, make_mono8_msg(header, gray), t)
            stats["cam_out"] += 1
        print("  ... kept %d cam frames, %d imu" % (stats["cam_out"], stats["imu_n"]))

    with rosbag.Bag(args.output, "w") as out, \
            ThreadPoolExecutor(max_workers=jobs) as pool:
        batch = []
        for topic, msg, t in inbag.read_messages(topics=read_topics):
            if topic in IMU_IN_CANDIDATES:
                # IMU 即时写出。rosbag 不要求按时间单调写入(读取时按索引重排),
                # 因此无需为 IMU 打断相机批 -> 相机批可攒满, 充分并行解码。
                out.write(IMU_OUT, msg, t)
                stats["imu_n"] += 1
                continue

            # camera
            stats["cam_in"] += 1
            out_topic = CAM_MAP[topic]
            if min_dt > 0.0:
                stamp = msg.header.stamp.to_sec()
                prev = last_kept.get(out_topic)
                if prev is not None and (stamp - prev) < min_dt:
                    continue  # 抽帧丢弃
                last_kept[out_topic] = stamp

            # 仅复制需要的字段交给线程池解码 (msg 不可跨迭代持有)
            batch.append((out_topic, msg.header, msg.data, t))
            if len(batch) >= args.batch:
                flush(out, pool, batch)
                batch = []

        flush(out, pool, batch)

    inbag.close()
    print("[convert] done: cam_in=%d cam_out=%d imu=%d err=%d" %
          (stats["cam_in"], stats["cam_out"], stats["imu_n"], stats["err"]))
    if stats["cam_out"] == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
