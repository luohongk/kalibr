#!/usr/bin/env python3
"""
convert_to_kalibr.py  (运行在 Docker 容器内, 使用原生 ROS rosbag + cv2)

把一个 calibration.bag 转成 Kalibr 输入 bag:
  - 4 路 CompressedImage(JPEG) -> sensor_msgs/Image(mono8), topic 改名 /cam0../cam3
  - 相机降采样到约 --cam-hz (默认 10Hz), 每路相机按 header 时间戳独立抽帧
  - IMU 原样透传, topic 改名 /imu0 (保留全频率, 用于 cam-imu 标定)

用法:
  python3 convert_to_kalibr.py --input calibration.bag --output kalibr_input.bag --cam-hz 10
"""
import argparse
import struct
import sys

import cv2
import numpy as np
import rosbag
import rospy
from sensor_msgs.msg import Image

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cam-hz", type=float, default=10.0,
                    help="相机目标抽帧频率 (默认 10Hz; <=0 表示不降采样)")
    args = ap.parse_args()

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
    cam_in = cam_out = imu_n = err = 0

    print("[convert] input=%s" % args.input)
    print("[convert] cam topics: %s" % cam_topics_present)
    print("[convert] imu topic : %s" % (imu_topic_present or "<none>"))
    print("[convert] cam target: %.1f Hz (min_dt=%.4f)" % (args.cam_hz, min_dt))

    with rosbag.Bag(args.output, "w") as out:
        for topic, msg, t in inbag.read_messages(topics=read_topics):
            if topic in IMU_IN_CANDIDATES:
                out.write(IMU_OUT, msg, t)
                imu_n += 1
                continue

            # camera
            cam_in += 1
            out_topic = CAM_MAP[topic]
            stamp = msg.header.stamp.to_sec()
            prev = last_kept.get(out_topic)
            if prev is not None and (stamp - prev) < min_dt:
                continue  # 抽帧丢弃

            buf = np.frombuffer(msg.data, dtype=np.uint8)
            gray = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
            if gray is None:
                err += 1
                continue
            out.write(out_topic, make_mono8_msg(msg.header, gray), t)
            last_kept[out_topic] = stamp
            cam_out += 1
            if cam_out % 2000 == 0:
                print("  ... kept %d cam frames, %d imu" % (cam_out, imu_n))

    inbag.close()
    print("[convert] done: cam_in=%d cam_out=%d imu=%d err=%d" %
          (cam_in, cam_out, imu_n, err))
    if cam_out == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
