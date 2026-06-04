#!/usr/bin/env python3
"""
layoffs_rosbag.py
=================
裁剪 ROS1 bag 文件，保留指定时间段内的消息，输出为新 bag。

依赖：
  pip install rosbags

用法示例：
  # 保留第 10s ~ 60s 的数据（相对于 bag 起始时间）
  python layoffs_rosbag.py -i input.bag -o output.bag --start 10 --end 60

  # 使用绝对时间戳（Unix 秒）
  python layoffs_rosbag.py -i input.bag -o output.bag --start 1777259940 --end 1777260000 --absolute
  python layoffs_rosbag.py -i ../20260428-145923.bag -o output.bag --start 1777359612 --end 1777359785 --absolute 

  # 只裁剪特定 topic
  python layoffs_rosbag.py -i input.bag -o output.bag --start 5 --end 30 \\
      --topics /fisheye/left/image_raw/compressed /imu_data_raw

  # 先查看 bag 信息再决定时间段
  python layoffs_rosbag.py -i input.bag --info
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from rosbags.rosbag1 import Reader, Writer
except ImportError:
    print("[ERROR] 缺少 rosbags 库，请执行：pip install rosbags")
    sys.exit(1)

DEFAULT_BAG = str(Path(__file__).parent.parent / "input.bag")


# ─── 工具 ────────────────────────────────────────────────────────────────────

def ns_to_sec(ns: int) -> float:
    return ns * 1e-9

def sec_to_ns(s: float) -> int:
    return int(s * 1e9)

def fmt_time(ns: int) -> str:
    s = ns_to_sec(ns)
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:06.3f}  ({s:.3f}s)"


# ─── info ────────────────────────────────────────────────────────────────────

def cmd_info(bag_path: str):
    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    topic_type: dict[str, str] = {}

    with Reader(bag_path) as bag:
        start_ns = bag.start_time
        end_ns   = bag.end_time
        duration = ns_to_sec(end_ns - start_ns)
        for c in bag.connections:
            topic_type[c.topic] = c.msgtype
        for conn, ts, _ in bag.messages():
            counts[conn.topic] += 1

    print(f"\n{'='*65}")
    print(f"  文件    : {bag_path}")
    print(f"  起始    : {fmt_time(start_ns)}")
    print(f"  结束    : {fmt_time(end_ns)}")
    print(f"  时长    : {duration:.3f} s")
    print(f"{'='*65}")
    print(f"  {'Topic':<46} {'消息数':>7}")
    print(f"  {'-'*46} {'-'*7}")
    for topic, msgtype in topic_type.items():
        print(f"  {topic:<46} {counts[topic]:>7}")
    print(f"{'='*65}\n")


# ─── trim ────────────────────────────────────────────────────────────────────

def cmd_trim(
    in_path: str,
    out_path: str,
    start_s: float,
    end_s: float,
    absolute: bool,
    topics: list[str] | None,
):
    out_p = Path(out_path)
    if out_p.exists():
        print(f"[ERROR] 输出文件已存在，请先删除或换名：{out_path}")
        sys.exit(1)

    out_p.parent.mkdir(parents=True, exist_ok=True)

    with Reader(in_path) as reader:
        bag_start_ns = reader.start_time
        bag_end_ns   = reader.end_time
        bag_duration = ns_to_sec(bag_end_ns - bag_start_ns)

        # 计算绝对时间窗口（ns）
        if absolute:
            win_start_ns = sec_to_ns(start_s)
            win_end_ns   = sec_to_ns(end_s)
        else:
            win_start_ns = bag_start_ns + sec_to_ns(start_s)
            win_end_ns   = bag_start_ns + sec_to_ns(end_s)

        # 边界检查
        if win_start_ns >= win_end_ns:
            print("[ERROR] --start 必须小于 --end")
            sys.exit(1)
        if win_end_ns <= bag_start_ns or win_start_ns >= bag_end_ns:
            print(f"[ERROR] 指定时间窗口与 bag 时间范围无交集")
            print(f"        bag  : [{ns_to_sec(bag_start_ns):.3f}s, {ns_to_sec(bag_end_ns):.3f}s]")
            print(f"        window: [{ns_to_sec(win_start_ns):.3f}s, {ns_to_sec(win_end_ns):.3f}s]")
            sys.exit(1)

        # 实际截取窗口（与 bag 范围求交）
        clip_start_ns = max(win_start_ns, bag_start_ns)
        clip_end_ns   = min(win_end_ns,   bag_end_ns)

        print(f"\n[INFO] 输入 bag  : {in_path}")
        print(f"[INFO] 输入时长  : {bag_duration:.3f} s")
        print(f"[INFO] 裁剪区间  : {ns_to_sec(clip_start_ns):.3f}s ~ {ns_to_sec(clip_end_ns):.3f}s")
        print(f"[INFO] 裁剪时长  : {ns_to_sec(clip_end_ns - clip_start_ns):.3f} s")
        if topics:
            print(f"[INFO] 保留 topic: {topics}")
        print(f"[INFO] 输出 bag  : {out_path}\n")

        # 筛选 connection
        selected_conns = [
            c for c in reader.connections
            if (topics is None or c.topic in topics)
        ]
        if not selected_conns:
            print("[ERROR] 没有匹配的 topic，请检查 --topics 参数")
            sys.exit(1)

        with Writer(out_path) as writer:
            # 建立 旧connection → 新connection 映射
            conn_map: dict[int, object] = {}
            for c in selected_conns:
                new_conn = writer.add_connection(
                    topic    = c.topic,
                    msgtype  = c.msgtype,
                    msgdef   = c.msgdef[1] if isinstance(c.msgdef, tuple) else c.msgdef,
                    md5sum   = c.digest,
                    callerid = c.ext.callerid,
                    latching = c.ext.latching,
                )
                conn_map[c.id] = new_conn

            # 遍历消息，按时间窗口过滤写入
            written = 0
            skipped = 0
            topic_counts: dict[str, int] = {}

            for conn, ts, raw in reader.messages(connections=selected_conns):
                if ts < clip_start_ns:
                    skipped += 1
                    continue
                if ts > clip_end_ns:
                    skipped += 1
                    continue
                writer.write(conn_map[conn.id], ts, raw)
                written += 1
                topic_counts[conn.topic] = topic_counts.get(conn.topic, 0) + 1

                if written % 500 == 0:
                    pct = (ts - clip_start_ns) / (clip_end_ns - clip_start_ns) * 100
                    print(f"  已写入 {written:>6} 条  [{pct:5.1f}%]", end="\r", flush=True)

    print(f"\n[DONE] 写入完成！共 {written} 条消息（跳过 {skipped} 条）")
    print(f"       输出文件大小: {os.path.getsize(out_path) / 1024 / 1024:.2f} MB")
    print(f"\n  {'Topic':<46} {'写入数':>7}")
    print(f"  {'-'*46} {'-'*7}")
    for t, n in sorted(topic_counts.items()):
        print(f"  {t:<46} {n:>7}")
    print()


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="裁剪 ROS1 bag 文件，保留指定时间段",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-i", "--input",  default=DEFAULT_BAG,
                        help=f"输入 bag 文件路径（默认: {DEFAULT_BAG}）")
    parser.add_argument("-o", "--output", default=None,
                        help="输出 bag 文件路径（默认: <输入名>_trimmed.bag）")
    parser.add_argument("--start", type=float, default=0.0,
                        help="裁剪起始时间（相对秒数，或 --absolute 时为 Unix 秒）")
    parser.add_argument("--end",   type=float, default=None,
                        help="裁剪结束时间（相对秒数，或 --absolute 时为 Unix 秒）")
    parser.add_argument("--absolute", action="store_true",
                        help="--start/--end 解释为绝对 Unix 时间戳（秒）")
    parser.add_argument("--topics", nargs="+", default=None, metavar="TOPIC",
                        help="只保留指定 topic（默认保留全部）")
    parser.add_argument("--info", action="store_true",
                        help="只打印 bag 信息，不裁剪")

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"[ERROR] 输入文件不存在: {args.input}")
        sys.exit(1)

    if args.info:
        cmd_info(args.input)
        return

    # 确定 end 时间
    if args.end is None:
        with Reader(args.input) as bag:
            if args.absolute:
                args.end = ns_to_sec(bag.end_time)
            else:
                args.end = ns_to_sec(bag.end_time - bag.start_time)

    # 自动生成输出文件名
    if args.output is None:
        in_p = Path(args.input)
        args.output = str(in_p.parent / f"{in_p.stem}_trimmed{in_p.suffix}")

    cmd_trim(
        in_path  = args.input,
        out_path = args.output,
        start_s  = args.start,
        end_s    = args.end,
        absolute = args.absolute,
        topics   = args.topics,
    )


if __name__ == "__main__":
    main()
