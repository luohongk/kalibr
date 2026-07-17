#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只把某个采集目录下的 imu mcap 转换为单个 ROS1 .bag(不含相机)

用法:
    /home/lhk/miniconda3/bin/python3 convert_mcap_to_rosbag_imu.py --input-mcap /path/to/imu_0.mcap
    /home/lhk/miniconda3/bin/python3 convert_mcap_to_rosbag_imu.py [采集目录或 imu_0.mcap]
    输入省略时默认 DEFAULT_DIR。采集目录结构应为:
        <采集目录>/imu/imu_0.mcap
    直接传 mcap 文件时,输出到该 mcap 所在目录。

背景 / 关键点:
  * 源文件是 ROS2 录制的 mcap(CDR 序列化)。
  * 转换流程:mcap StreamReader 读原始 CDR 字节
      -> rosbags 反序列化 (ROS2 typestore)
      -> rosbags 重序列化为 ROS1
      -> 写入单个 rosbag1 .bag
  * 使用低层 StreamReader 顺序读取,兼容录制未正常收尾(缺结尾 magic /
    message index)的情况:遇到末尾截断时保留已读消息并平滑结束。

依赖(conda python 已装):mcap, rosbags
"""

import argparse
import os

from mcap.stream_reader import StreamReader
from mcap.records import Schema, Channel, Message

from rosbags.rosbag1 import Writer
from rosbags.typesys import Stores, get_typestore

DEFAULT_DIR = "/media/lhk/Extreme_SSD/lhk_data/新设备采集的mcap标定数据/collected_data/20260713-152157"
OUTPUT_IMU_TOPIC = "/imu_data_raw"
DEFAULT_MCAP_OUT_BAG_NAME = "imu.bag"


def main():
    parser = argparse.ArgumentParser(
        description="只把 ROS2 imu mcap 转换为 ROS1 bag，输出 topic 固定为 /imu_data_raw"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="采集目录或 imu_0.mcap 文件路径",
    )
    parser.add_argument(
        "-i",
        "--input-mcap",
        help="imu_0.mcap 文件路径；优先级高于位置参数",
    )
    parser.add_argument(
        "--out-name",
        help="输出 bag 文件名；直接传 mcap 时默认 imu.bag，传目录时默认 <目录名>.bag",
    )
    args = parser.parse_args()

    input_path = os.path.abspath(args.input_mcap or args.input or DEFAULT_DIR)
    if os.path.isfile(input_path):
        mcap_path = input_path
        output_dir = os.path.dirname(mcap_path)
        out_name = args.out_name or DEFAULT_MCAP_OUT_BAG_NAME
    else:
        base = input_path
        mcap_path = os.path.join(base, "imu", "imu_0.mcap")
        output_dir = base
        out_name = args.out_name or f"{os.path.basename(base.rstrip('/'))}.bag"

    sources = [("imu", mcap_path)]
    out_bag = os.path.join(output_dir, out_name)
    print(f"[输入] {mcap_path}")
    print(f"[输出目录] {output_dir}")
    print(f"[输出] {out_bag}")

    _convert(sources, out_bag)


def _convert(SOURCES, OUT_BAG):
    ts2 = get_typestore(Stores.ROS2_HUMBLE)
    ts1 = get_typestore(Stores.ROS1_NOETIC)

    if os.path.exists(OUT_BAG):
        os.remove(OUT_BAG)

    total = 0
    # topic -> {"type": str, "count": int, "t_min": ns, "t_max": ns}
    topic_stats = {}
    bag_t_min = None
    bag_t_max = None
    with Writer(OUT_BAG) as writer:
        conn_cache = {}  # (topic, ros1_type) -> connection

        for tag, path in SOURCES:
            if not os.path.exists(path):
                print(f"[跳过] 文件不存在: {path}")
                continue
            print(f"[读取] {tag}: {path}")

            schemas = {}
            channels = {}
            count = 0

            with open(path, "rb") as f:
                sr = StreamReader(f, emit_chunks=False)
                rec_iter = iter(sr.records)
                while True:
                    try:
                        rec = next(rec_iter)
                    except StopIteration:
                        break
                    except Exception as e:
                        # 录制未正常收尾,最后一个 chunk 被截断:保留已读取的消息,
                        # 忽略末尾残缺数据并结束本文件
                        print(f"    [提示] {tag} 末尾数据截断,已停止读取该文件: {type(e).__name__}: {e}")
                        break
                    if isinstance(rec, Schema):
                        schemas[rec.id] = rec
                    elif isinstance(rec, Channel):
                        channels[rec.id] = rec
                    elif isinstance(rec, Message):
                        ch = channels[rec.channel_id]
                        sc = schemas[ch.schema_id]

                        ros2_type = sc.name                       # e.g. sensor_msgs/msg/Imu
                        # rosbags 两个 typestore 内部都用 pkg/msg/Type 命名
                        typename = ros2_type
                        topic = OUTPUT_IMU_TOPIC

                        # 反序列化 (ROS2 CDR) -> 消息对象
                        msg = ts2.deserialize_cdr(rec.data, typename)
                        # ROS1 的 std_msgs/Header 比 ROS2 多一个 seq 字段,补上
                        hdr = getattr(msg, "header", None)
                        if hdr is not None:
                            hdr.seq = 0
                        # 重序列化为 ROS1
                        raw = ts1.serialize_ros1(msg, typename)

                        key = (topic, typename)
                        conn = conn_cache.get(key)
                        if conn is None:
                            conn = writer.add_connection(
                                topic,
                                typename,
                                typestore=ts1,
                            )
                            conn_cache[key] = conn

                        # mcap Message.log_time 为纳秒
                        t = rec.log_time
                        writer.write(conn, t, raw)
                        count += 1
                        total += 1

                        st = topic_stats.get(topic)
                        if st is None:
                            st = {"type": typename, "count": 0, "t_min": t, "t_max": t}
                            topic_stats[topic] = st
                        st["count"] += 1
                        if t < st["t_min"]:
                            st["t_min"] = t
                        if t > st["t_max"]:
                            st["t_max"] = t
                        if bag_t_min is None or t < bag_t_min:
                            bag_t_min = t
                        if bag_t_max is None or t > bag_t_max:
                            bag_t_max = t

                        if count % 5000 == 0:
                            print(f"    {tag}: {count} 条...")

            print(f"[完成] {tag}: 共 {count} 条")

    print(f"\n全部完成,总计 {total} 条消息 -> {OUT_BAG}")

    # 打印每个 topic 的时间与数量信息
    print("\n[Topic 统计]")
    for topic in sorted(topic_stats):
        st = topic_stats[topic]
        t0 = st["t_min"] / 1e9
        t1 = st["t_max"] / 1e9
        dur = t1 - t0
        rate = (st["count"] - 1) / dur if dur > 0 else 0.0
        print(f"  {topic}")
        print(f"      类型:   {st['type']}")
        print(f"      数量:   {st['count']}")
        print(f"      起始:   {t0:.9f}")
        print(f"      结束:   {t1:.9f}")
        print(f"      时长:   {dur:.3f} s")
        print(f"      频率:   {rate:.2f} Hz")

    if bag_t_min is not None:
        bt0 = bag_t_min / 1e9
        bt1 = bag_t_max / 1e9
        print(f"\n[Bag 时间范围] {bt0:.9f} ~ {bt1:.9f}  (时长 {bt1 - bt0:.3f} s)")


if __name__ == "__main__":
    main()
