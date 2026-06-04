#!/usr/bin/env python3
"""
print_imu.py
============
从 ROS1 bag 文件中读取并打印 IMU 数据（无需 ROS 环境）。

依赖：
  pip install rosbags

用法：
  python print_imu.py                        # 使用默认 bag，打印前 50 条
  python print_imu.py --bag other.bag        # 指定 bag 文件
  python print_imu.py --all                  # 打印全部 IMU 数据
  python print_imu.py --lines 200            # 打印前 200 条
  python print_imu.py --topic /my_imu        # 指定 IMU topic
  python print_imu.py --csv imu.csv          # 同时导出为 CSV 文件
"""

import argparse
import csv
import sys
from pathlib import Path

try:
    from rosbags.rosbag1 import Reader
    from rosbags.typesys import get_typestore, Stores
except ImportError:
    print("[ERROR] 缺少 rosbags 库，请执行：pip install rosbags")
    sys.exit(1)

# ─── 默认配置 ─────────────────────────────────────────────────────────────────
DEFAULT_BAG   = str(Path(__file__).parent.parent / "input.bag")
DEFAULT_TOPIC = "/imu_data_raw"


def ns_to_sec(ns: int) -> float:
    return ns * 1e-9


def print_imu(bag_path: str,
              topic: str = DEFAULT_TOPIC,
              max_lines: int = 50,
              print_all: bool = False,
              csv_path: str | None = None):
    """
    读取 bag 中的 IMU 数据并打印到终端，可选导出 CSV。

    参数
    ----
    bag_path  : bag 文件路径
    topic     : IMU topic 名称
    max_lines : 最多打印条数（print_all=True 时忽略）
    print_all : 为 True 时打印全部数据
    csv_path  : 若非 None，同时将数据写入该 CSV 文件
    """
    typestore = get_typestore(Stores.ROS1_NOETIC)

    header = (f"  {'#':>6}  {'时间戳(s)':>14}  "
              f"{'ax(m/s²)':>12} {'ay(m/s²)':>12} {'az(m/s²)':>12}  "
              f"{'wx(rad/s)':>12} {'wy(rad/s)':>12} {'wz(rad/s)':>12}")
    sep    = "  " + "-" * (len(header) - 2)

    print(f"\n{'='*90}")
    print(f"  Bag   : {bag_path}")
    print(f"  Topic : {topic}")
    print(f"  条数  : {'全部' if print_all else max_lines}")
    print(f"{'='*90}")
    print(header)
    print(sep)

    csv_file   = None
    csv_writer = None
    if csv_path:
        csv_file   = open(csv_path, "w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["#", "timestamp_s",
                              "ax", "ay", "az",
                              "wx", "wy", "wz"])

    count      = 0
    first_ts   = None
    last_ts    = None

    try:
        with Reader(bag_path) as bag:
            imu_conns = [c for c in bag.connections if c.topic == topic]
            if not imu_conns:
                print(f"\n[WARN] 未找到 topic: {topic}")
                print(f"       bag 中可用 topic: "
                      f"{sorted({c.topic for c in bag.connections})}")
                return

            for conn, ts, raw in bag.messages(connections=imu_conns):
                if not print_all and count >= max_lines:
                    break

                msg = typestore.deserialize_ros1(raw, conn.msgtype)
                la  = msg.linear_acceleration
                av  = msg.angular_velocity
                ts_s = ns_to_sec(ts)

                if first_ts is None:
                    first_ts = ts_s
                last_ts = ts_s

                print(f"  {count+1:>6}  {ts_s:>14.4f}  "
                      f"{la.x:>12.5f} {la.y:>12.5f} {la.z:>12.5f}  "
                      f"{av.x:>12.5f} {av.y:>12.5f} {av.z:>12.5f}")

                if csv_writer:
                    csv_writer.writerow([count + 1, f"{ts_s:.6f}",
                                         f"{la.x:.6f}", f"{la.y:.6f}", f"{la.z:.6f}",
                                         f"{av.x:.6f}", f"{av.y:.6f}", f"{av.z:.6f}"])
                count += 1
    finally:
        if csv_file:
            csv_file.close()

    # ── 统计摘要 ──────────────────────────────────────────────────────────────
    print(sep)
    print(f"\n  共打印 {count} 条 IMU 数据")
    if first_ts is not None and last_ts is not None and count > 1:
        duration = last_ts - first_ts
        avg_hz   = (count - 1) / duration if duration > 0 else float("nan")
        print(f"  时间跨度: {first_ts:.4f}s → {last_ts:.4f}s  ({duration:.4f}s)")
        print(f"  平均频率: {avg_hz:.1f} Hz")
    if csv_path:
        print(f"  CSV 已保存: {csv_path}")
    print()


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="从 ROS1 bag 文件打印 IMU 数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--bag", default=DEFAULT_BAG,
                        help=f"bag 文件路径（默认: {DEFAULT_BAG}）")
    parser.add_argument("--topic", default=DEFAULT_TOPIC,
                        help=f"IMU topic（默认: {DEFAULT_TOPIC}）")
    parser.add_argument("--lines", type=int, default=50,
                        help="打印前 N 条（默认 50，被 --all 覆盖）")
    parser.add_argument("--all", action="store_true",
                        help="打印全部 IMU 数据")
    parser.add_argument("--csv", metavar="FILE",
                        help="同时导出为 CSV 文件")

    args = parser.parse_args()

    import os
    if not os.path.isfile(args.bag):
        print(f"[ERROR] bag 文件不存在: {args.bag}")
        sys.exit(1)

    print_imu(
        bag_path  = args.bag,
        topic     = args.topic,
        max_lines = args.lines,
        print_all = args.all,
        csv_path  = args.csv,
    )


if __name__ == "__main__":
    main()
