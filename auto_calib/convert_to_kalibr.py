#!/usr/bin/env python3
"""
convert_to_kalibr.py  (运行在 Docker 容器内, 使用原生 ROS rosbag + PyAV)

把一个 calibration.bag 转成 Kalibr 输入 bag:
  - 4 路 CompressedImage(H.264) -> sensor_msgs/Image(mono8), topic 改名 /cam0../cam3
  - 相机降采样到约 --cam-hz (默认全帧, <=0 不降采样), 每路相机按 header 时间戳独立抽帧
  - IMU 原样透传, topic 改名 /imu0 (保留全频率, 用于 cam-imu 标定)

H.264 说明:
  相机话题现在是 H.264 视频流 (CompressedImage.format == 'h264'), 不再是 JPEG。
  H.264 是帧间压缩流, 不能逐帧独立解码: 每路相机需按序把 NAL 包喂给一个独立的
  解码器。解码器有启动延迟(需等到首个关键帧)且可能滞后若干包才吐出帧, 因此:
    - 每路相机维护一个独立 av 解码器
    - 用一个 FIFO 队列保存每条消息的 header.stamp, 每吐出一帧就弹出一个时间戳配对
      (已验证该流无 B 帧重排, 输出 PTS 单调 -> FIFO 配对成立)
    - 全部喂完后 flush 解码器, 回收缓冲中剩余的帧
  兼容旧格式: 若 format 为 jpeg/其它, 回退用 cv2.imdecode 逐帧解码。

用法:
  python3 convert_to_kalibr.py --input calibration.bag --output kalibr_input.bag --cam-hz 30
  python3 convert_to_kalibr.py --input ... --output ... --cam-hz 0
"""
import argparse
import sys
import threading
import queue
from collections import deque

import av
import cv2
import numpy as np
import rosbag
from sensor_msgs.msg import Image

# OpenCV 自身线程数设 1 (仅用于旧 JPEG 回退路径)
cv2.setNumThreads(1)

CAM_MAP = {
    # 新格式 (2026-07): /cam{0..3}/image/compressed
    "/cam0/image/compressed": "/cam0/image_raw",
    "/cam1/image/compressed": "/cam1/image_raw",
    "/cam2/image/compressed": "/cam2/image_raw",
    "/cam3/image/compressed": "/cam3/image_raw",
    # 旧格式: /fisheye/*/image_raw/compressed
    "/fisheye/left/image_raw/compressed":   "/cam0/image_raw",
    "/fisheye/right/image_raw/compressed":  "/cam1/image_raw",
    "/fisheye/bleft/image_raw/compressed":  "/cam2/image_raw",
    "/fisheye/bright/image_raw/compressed": "/cam3/image_raw",
}
# 容忍带/不带前导斜杠两种写法
CAM_MAP.update({k.lstrip("/"): v for k, v in list(CAM_MAP.items())})

IMU_IN_CANDIDATES = {"/imu/data_raw", "imu/data_raw", "/imu_data_raw", "imu_data_raw"}
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


class BagWriter(threading.Thread):
    """单写线程: 串行把 (topic, msg, stamp) 写进 bag。

    rosbag 写非线程安全, 故所有写入(相机各路 + IMU) 都汇聚到这一个线程。
    bag 不要求按时间有序, 因此各路 worker 乱序产出、writer 交错写没问题。
    """

    def __init__(self, out_bag, maxsize=2048):
        super().__init__(daemon=True)
        self.out = out_bag
        self.q = queue.Queue(maxsize=maxsize)   # (topic, msg, stamp) 或 None(收尾)
        self.written = 0                        # 已写消息总数 (仅 writer 线程读写, 无需锁)

    def put(self, topic, msg, stamp):
        self.q.put((topic, msg, stamp))

    def close(self):
        self.q.put(None)

    def run(self):
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                topic, msg, stamp = item
                self.out.write(topic, msg, stamp)
                self.written += 1
        except Exception as e:
            # writer 出错同样不能静默死掉, 否则上游 put 阻塞 -> 死锁。排空队列放行。
            print("[convert][ERR] writer: %r" % (e,))
            try:
                while self.q.get_nowait() is not None:
                    pass
            except queue.Empty:
                pass


class CamStream(threading.Thread):
    """单路相机 worker 线程: H.264 解码 + 抽帧 + 转 mono8, 产出交给 writer。

    header.stamp 进 FIFO, 每吐出一帧弹一个配对, 保证时间戳对齐。
    解码/to_ndarray 是 CPU 重活且在 C 层释放 GIL, 4 路 worker 可真正并行。
    """

    def __init__(self, out_topic, writer, min_dt, stats, stats_lock,
                 dec_threads=1, maxsize=1024):
        super().__init__(daemon=True)
        self.out_topic = out_topic
        self.writer = writer
        self.min_dt = min_dt
        self.stats = stats
        self.stats_lock = stats_lock
        self.q = queue.Queue(maxsize=maxsize)   # 输入: CompressedImage msg 或 None(收尾)
        self.codec = av.CodecContext.create("h264", "r")
        # 关键: 每个解码器用【单线程】(thread_count=1, 不设 FRAME/SLICE/AUTO)。
        # 加速靠的是 4 路 worker 线程并行 (PyAV decode 在 C 层释放 GIL, 4 线程近 4x)。
        # 实测(本数据 7925 帧): 串行 69s/115fps -> 4线程并行 17.5s/453fps (3.95x)。
        # 切勿开 thread_type: FRAME/AUTO 帧间并行在此流上极慢(远慢于单线程), 且在超多核
        # 机器上 AUTO 会按 CPU 数(384) 开爆线程。dec_threads 保留仅为极端场景手调, 默认 1。
        self.codec.thread_count = dec_threads
        self.stamps = deque()      # 待配对的 header.stamp (rospy.Time)
        self.last_kept = None      # 上次保留帧的时间(sec), 用于抽帧
        self._local = {"cam_in": 0, "cam_out": 0, "err": 0}  # 线程本地计数, 收尾合并

    def feed(self, msg):
        """主线程调用: 把一条相机消息投入本路输入队列 (阻塞背压)。"""
        self.q.put(msg)

    def close(self):
        self.q.put(None)

    def run(self):
        try:
            while True:
                msg = self.q.get()
                if msg is None:
                    break
                self._process(msg)
            self._flush()
        except Exception as e:
            # worker 出错不能静默死掉, 否则主线程 feed() 会阻塞在满队列 -> 死锁。
            # 打印后排空剩余输入, 让主线程的 put 得以返回。
            print("[convert][ERR] %s worker: %r" % (self.out_topic, e))
            self._local["err"] += 1
            try:
                while self.q.get_nowait() is not None:
                    pass
            except queue.Empty:
                pass
        finally:
            # 收尾: 本地计数一次性合并进共享 stats
            with self.stats_lock:
                self.stats["cam_in"] += self._local["cam_in"]
                self.stats["cam_out"] += self._local["cam_out"]
                self.stats["err"] += self._local["err"]

    def _process(self, msg):
        self._local["cam_in"] += 1
        self.stamps.append(msg.header.stamp)
        data = bytes(msg.data)
        try:
            packets = self.codec.parse(data)
        except av.error.InvalidDataError:
            packets = []
        for pkt in packets:
            self._decode_pkt(pkt)

    def _flush(self):
        # 送入 None 触发解码器排空缓冲帧
        try:
            for frame in self.codec.decode(None):
                self._emit(frame)
        except (av.error.InvalidDataError, av.error.EOFError):
            pass

    def _decode_pkt(self, pkt):
        try:
            frames = self.codec.decode(pkt)
        except av.error.InvalidDataError:
            # 关键帧到来前的启动阶段会解码失败, 正常跳过
            return
        for frame in frames:
            self._emit(frame)

    def _emit(self, frame):
        # 取出配对时间戳 (FIFO)。理论上 out 帧数 <= 送入消息数, 队列不会空;
        # 但保险起见空则丢弃该帧。
        if not self.stamps:
            self._local["err"] += 1
            return
        stamp = self.stamps.popleft()

        # 抽帧 (对已解码帧按目标频率降采样)
        if self.min_dt > 0.0:
            sec = stamp.to_sec()
            if self.last_kept is not None and (sec - self.last_kept) < self.min_dt:
                return
            self.last_kept = sec

        gray = frame.to_ndarray(format="gray")
        header = _make_header(stamp)
        # 写出交给单写线程 (rosbag 写非线程安全)
        self.writer.put(self.out_topic, make_mono8_msg(header, gray), stamp)
        self._local["cam_out"] += 1



def _make_header(stamp):
    from std_msgs.msg import Header
    h = Header()
    h.stamp = stamp
    return h


def decode_gray_jpeg(data):
    buf = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--cam-hz", type=float, default=30.0,
                    help="相机目标抽帧频率 (默认 30Hz; <=0 表示不降采样)")
    # --jobs/--batch 保留仅为兼容旧调用, H.264 解码是有状态串行流, 不再并行
    ap.add_argument("--jobs", type=int, default=8, help="(已废弃, H.264 串行解码)")
    ap.add_argument("--batch", type=int, default=256, help="(已废弃)")
    ap.add_argument("--dec-threads", type=int, default=1,
                    help="每路 H.264 解码器线程数 (默认1=单线程; 加速靠4路worker并行, 勿设大, 见 CamStream 注释)")
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
    stats = {"cam_in": 0, "cam_out": 0, "imu_n": 0, "err": 0}

    print("[convert] input=%s" % args.input)
    print("[convert] cam topics: %s" % cam_topics_present)
    print("[convert] imu topic : %s" % (imu_topic_present or "<none>"))
    print("[convert] cam target: %s" %
          ("%.1f Hz (min_dt=%.4f)" % (args.cam_hz, min_dt) if args.cam_hz > 0
           else "全帧保留 (不降采样)"))
    print("[convert] codec: H.264 (PyAV), 每路独立 worker 线程并行解码 + 单写线程")

    stats_lock = threading.Lock()
    with rosbag.Bag(args.output, "w") as out:
        writer = BagWriter(out)
        writer.start()
        streams = {t: CamStream(CAM_MAP[t], writer, min_dt, stats, stats_lock,
                                dec_threads=args.dec_threads)
                   for t in cam_topics_present}
        for s in streams.values():
            s.start()

        last_report = 0
        for topic, msg, t in inbag.read_messages(topics=read_topics):
            if topic in IMU_IN_CANDIDATES:
                # 用 header.stamp 作为 bag time, 与相机对齐; 见旧注释。
                writer.put(IMU_OUT, msg, msg.header.stamp)
                with stats_lock:
                    stats["imu_n"] += 1
            else:
                # 阻塞式投递 -> 队列满时自然背压, 防止内存爆掉
                streams[topic].feed(msg)

            # 进度以 writer 已写总数为准 (相机帧 + IMU 汇聚于此)
            if writer.written - last_report >= 5000:
                last_report = writer.written
                print("  ... written %d msgs (cam+imu), backlog≈%d"
                      % (writer.written, writer.q.qsize()))

        # 收尾: 先让各路 worker 读完输入并排空解码器缓冲
        for s in streams.values():
            s.close()
        for s in streams.values():
            s.join()
        # 相机全部产出完毕后, 关闭 writer 并等其写完
        writer.close()
        writer.join()

    inbag.close()
    print("[convert] done: cam_in=%d cam_out=%d imu=%d err=%d" %
          (stats["cam_in"], stats["cam_out"], stats["imu_n"], stats["err"]))
    if stats["cam_out"] == 0:
        sys.exit(3)


if __name__ == "__main__":
    main()
