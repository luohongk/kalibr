#!/usr/bin/env python3
"""
将 input_move_camera_imu.bag → kalibr_input_imu.bag
CompressedImage (JPEG) → sensor_msgs/Image (mono8 灰度)
IMU 原样透传，仅重命名 topic

性能：
  - JPEG 解码用 ThreadPoolExecutor 并行（cv2.imdecode 会释放 GIL）。
  - 主线程顺序读取 + 顺序写入，保证 bag 时间戳单调。
  - 用有界 deque 限流，防止解码 future 堆积爆内存。
  - tqdm 实时进度条。

用法：
  python to_kalibr.py                        # 默认 workers=cpu/2
  python to_kalibr.py --workers 8            # 指定线程数
  python to_kalibr.py --force                # 覆盖已存在的输出
"""
import argparse
import os
import struct
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

import cv2
import numpy as np
from rosbags.rosbag1 import Reader, Writer
from rosbags.typesys import get_typestore, Stores
from tqdm import tqdm

INPUT_BAG  = Path(__file__).parent / "input_move_camera_imu.bag"
OUTPUT_BAG = Path(__file__).parent / "kalibr_input_imu.bag"

CAM_MAP = {
    "/fisheye/left/image_raw/compressed":   "/cam0/image_raw",
    "/fisheye/right/image_raw/compressed":  "/cam1/image_raw",
    "/fisheye/bleft/image_raw/compressed":  "/cam2/image_raw",
    "/fisheye/bright/image_raw/compressed": "/cam3/image_raw",
}
IMU_IN  = "/imu_data_raw"
IMU_OUT = "/imu0"


# ── 解码 / 打包 ────────────────────────────────────────────────────────────
def decode_compressed(raw: bytes):
    """从 ROS1 CompressedImage 二进制中提取 JPEG 并解码为灰度图。
    返回 (seq, secs, nsec, gray)。
    布局: seq(4) secs(4) nsec(4) frame_id_len(4)+str  format_len(4)+str  data_len(4)+data
    """
    o = 0
    seq,       = struct.unpack_from('<I',  raw, o); o += 4
    secs, nsec = struct.unpack_from('<II', raw, o); o += 8
    fid_len,   = struct.unpack_from('<I',  raw, o); o += 4 + fid_len
    fmt_len,   = struct.unpack_from('<I',  raw, o); o += 4 + fmt_len
    data_len,  = struct.unpack_from('<I',  raw, o); o += 4
    buf = np.frombuffer(raw, dtype=np.uint8, offset=o, count=data_len).copy()
    gray = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    return seq, secs, nsec, gray


def pack_image(seq, secs, nsec, frame_id: str, gray: np.ndarray) -> bytes:
    """序列化为 ROS1 sensor_msgs/Image (mono8)。"""
    h, w = gray.shape
    fid, enc = frame_id.encode(), b"mono8"
    return (struct.pack('<I', seq) +
            struct.pack('<II', secs, nsec) +
            struct.pack('<I', len(fid)) + fid +
            struct.pack('<II', h, w) +
            struct.pack('<I', len(enc)) + enc +
            struct.pack('<B', 0) +
            struct.pack('<I', w) +
            struct.pack('<I', h * w) + gray.tobytes())


def process_cam(raw: bytes, frame_id: str) -> bytes:
    """在 worker 线程里跑：解码 + 重新打包，输出可直接 writer.write 的字节串。
    抛异常会被主线程通过 future.result() 捕获。
    """
    seq, secs, nsec, gray = decode_compressed(raw)
    if gray is None:
        raise ValueError("cv2.imdecode 返回 None（JPEG 损坏？）")
    return pack_image(seq, secs, nsec, frame_id, gray)


# ── 主流程 ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input",  default=str(INPUT_BAG),  help=f"输入 bag (默认 {INPUT_BAG.name})")
    ap.add_argument("--output", default=str(OUTPUT_BAG), help=f"输出 bag (默认 {OUTPUT_BAG.relative_to(Path(__file__).parent)})")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                    help="解码线程数（默认 cpu_count/2）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的输出")
    args = ap.parse_args()

    in_path  = Path(args.input)
    out_path = Path(args.output)

    if not in_path.exists():
        print(f"[ERROR] 输入文件不存在: {in_path}"); sys.exit(1)
    if out_path.exists():
        if args.force:
            out_path.unlink()
        else:
            print(f"[ERROR] 输出文件已存在，请先删除或加 --force: {out_path}"); sys.exit(1)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    typestore = get_typestore(Stores.ROS1_NOETIC)
    ok = err = imu_count = 0

    # 有界并发：最多缓存 workers*4 个解码任务，防止内存爆炸
    max_inflight = args.workers * 4

    with Reader(str(in_path)) as reader, \
         Writer(str(out_path)) as writer, \
         ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="dec") as pool:

        cam_conns = [c for c in reader.connections if c.topic in CAM_MAP]
        imu_conns = [c for c in reader.connections if c.topic == IMU_IN]

        if not cam_conns:
            print(f"[ERROR] 输入 bag 中找不到任何相机 topic: {list(CAM_MAP)}"); sys.exit(1)

        # 注册输出 topic
        out_cam = {
            c.topic: writer.add_connection(CAM_MAP[c.topic], "sensor_msgs/msg/Image",
                                           typestore=typestore)
            for c in cam_conns
        }
        out_imu = None
        if imu_conns:
            ic = imu_conns[0]
            out_imu = writer.add_connection(
                IMU_OUT, ic.msgtype,
                msgdef   = ic.msgdef[1] if isinstance(ic.msgdef, tuple) else ic.msgdef,
                md5sum   = ic.digest,
                callerid = getattr(ic.ext, "callerid", ""),
                latching = getattr(ic.ext, "latching", 0),
            )

        # 预先算 frame_id 映射，省得在热路径里反复 strip/replace
        frame_id_of = {topic: out_topic.lstrip("/").replace("/image_raw", "")
                       for topic, out_topic in CAM_MAP.items()}

        # 进度：相机 + IMU 总条数
        total_cam = sum(c.msgcount for c in cam_conns)
        total_imu = sum(c.msgcount for c in imu_conns)
        total = total_cam + total_imu

        # pending 中的元素是元组：
        #   ("cam", topic, ts, Future[bytes])
        #   ("imu", ts, raw)
        pending: deque = deque()

        def drain_one(pbar: tqdm):
            """从 pending 头部取一条写入 bag。阻塞等待 future 完成（保序）。"""
            nonlocal ok, err, imu_count
            head = pending.popleft()
            if head[0] == "imu":
                _, ts, raw = head
                if out_imu is not None:
                    writer.write(out_imu, ts, raw)
                imu_count += 1
            else:  # "cam"
                _, topic, ts, fut = head
                try:
                    payload = fut.result()
                    writer.write(out_cam[topic], ts, payload)
                    ok += 1
                except Exception as e:
                    pbar.write(f"[WARN] {topic} ts={ts}: {e}")
                    err += 1
            pbar.update(1)
            pbar.set_postfix(cam=ok, imu=imu_count, err=err, refresh=False)

        with tqdm(total=total, unit="msg", desc="转换中", smoothing=0.05) as pbar:
            for conn, ts, raw in reader.messages(connections=cam_conns + imu_conns):
                if conn.topic == IMU_IN:
                    pending.append(("imu", ts, raw))
                else:
                    fut: Future = pool.submit(process_cam, raw, frame_id_of[conn.topic])
                    pending.append(("cam", conn.topic, ts, fut))

                # 限流：超过阈值就先把队头写出去（保证写入按时间戳顺序）
                while len(pending) >= max_inflight:
                    drain_one(pbar)

            # 收尾
            while pending:
                drain_one(pbar)

    size_gb = out_path.stat().st_size / 1024**3
    print(f"\n完成: 相机 {ok} 帧 (失败 {err}), IMU {imu_count} 条")
    print(f"输出: {out_path}  ({size_gb:.2f} GB)")


if __name__ == "__main__":
    main()
