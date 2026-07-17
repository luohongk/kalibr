#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把某个采集目录下的 ROS2 mcap (cam0/1/2/3 + imu) 合并转换为单个 ROS1 .bag。

这个版本不把相机 CompressedImage 里的 H.264 数据转成 JPEG,只做:
    mcap 原始 CDR -> ROS2 消息对象 -> ROS1 序列化 -> rosbag1

用法:
    /home/lhk/miniconda3/bin/python3 convert_mcap_to_rosbag_new.py --input-dir 输入输出目录
    /home/lhk/miniconda3/bin/python3 convert_mcap_to_rosbag_new.py [输入输出目录]

默认使用统一 Kalibr 逻辑顺序:
    /cam0/image/compressed = bleft
    /cam1/image/compressed = left
    /cam2/image/compressed = right
    /cam3/image/compressed = bright
    /imu/data_raw

如果需要对齐旧设备 ROS1 bag 的 topic 名,加 --remap-topics。

依赖(conda python 已装):mcap, rosbags
"""

import argparse
import heapq
import itertools
import os
import queue
import threading
from dataclasses import dataclass

from mcap.records import Channel, Message, Schema
from mcap.stream_reader import StreamReader

from rosbags.rosbag1 import Writer
from rosbags.typesys import Stores, get_typestore

DEFAULT_DIR = "/media/lhk/Extreme_SSD/lhk_data/新设备采集的mcap标定数据/0714/20260714-105900"
DEFAULT_OUT_BAG_NAME = "calibration_new.bag"
QUEUE_SIZE = 32

KALIBR_TOPIC_REMAP = {
    # 新设备统一约定: cam0=bleft, cam1=left, cam2=right, cam3=bright.
    "/cam0/image/compressed": "/cam0/image/compressed",
    "/cam1/image/compressed": "/cam1/image/compressed",
    "/cam2/image/compressed": "/cam2/image/compressed",
    "/cam3/image/compressed": "/cam3/image/compressed",
    "/imu/data_raw": "/imu/data_raw",
}

LEGACY_TOPIC_REMAP = {
    "/cam0/image/compressed": "/fisheye/bleft/image_raw/compressed",
    "/cam1/image/compressed": "/fisheye/left/image_raw/compressed",
    "/cam2/image/compressed": "/fisheye/right/image_raw/compressed",
    "/cam3/image/compressed": "/fisheye/bright/image_raw/compressed",
    "/imu/data_raw": "/imu_data_raw",
}

CALLERIDS = {
    "/fisheye/left/image_raw/compressed": "/manager_camera",
    "/fisheye/right/image_raw/compressed": "/manager_camera",
    "/fisheye/bleft/image_raw/compressed": "/manager_camera",
    "/fisheye/bright/image_raw/compressed": "/manager_camera",
    "/imu_data_raw": "/manager_imu",
}


@dataclass
class ConvertedMessage:
    ts: int
    topic: str
    typename: str
    raw: bytes
    source_idx: int


@dataclass
class SourceDone:
    count: int
    error: Exception | None = None


def main():
    parser = argparse.ArgumentParser(
        description="把 ROS2 mcap(cam0/1/2/3 + imu) 直接转换为 ROS1 calibration.bag,不转 JPEG"
    )
    parser.add_argument(
        "capture_dir",
        nargs="?",
        help="输入输出目录；默认从该目录读取 mcap，并在该目录生成 calibration.bag",
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        help="输入输出目录；优先级高于位置参数",
    )
    parser.add_argument(
        "--output-root",
        help="只在该目录生成 calibration.bag；默认写到 capture_dir",
    )
    parser.add_argument(
        "--out-name",
        default=DEFAULT_OUT_BAG_NAME,
        help=f"输出 bag 文件名，默认 {DEFAULT_OUT_BAG_NAME}",
    )
    parser.add_argument(
        "--queue-size",
        type=int,
        default=QUEUE_SIZE,
        help=f"每个读取线程的缓冲消息数，默认 {QUEUE_SIZE}",
    )
    parser.add_argument(
        "--remap-topics",
        action="store_true",
        help="把原始 /cam*/image/compressed 和 /imu/data_raw 改成旧 ROS1 bag 的 /fisheye/* 与 /imu_data_raw",
    )
    args = parser.parse_args()

    base = args.input_dir or args.capture_dir or DEFAULT_DIR
    base = os.path.abspath(base)
    output_root = os.path.abspath(args.output_root) if args.output_root else base
    sources = _resolve_sources(base)
    os.makedirs(output_root, exist_ok=True)
    out_bag = os.path.join(output_root, args.out_name)

    print(f"[采集目录] {base}")
    print(f"[输出目录] {output_root}")
    print(f"[输出] {out_bag}")
    print("[模式] 直接写入 rosbag,不做 H.264 -> JPEG 转码")
    if args.remap_topics:
        topic_mode = "使用旧 ROS1 topic 映射"
    else:
        topic_mode = "使用统一 Kalibr 逻辑顺序: cam0=bleft, cam1=left, cam2=right, cam3=bright"
    print(f"[Topic] {topic_mode}")
    print("[并行] 每个 mcap 一个读取线程，主线程按时间戳归并写 bag")

    _convert(
        sources,
        out_bag,
        queue_size=max(1, args.queue_size),
        remap_topics=args.remap_topics,
    )


def _resolve_sources(base):
    sources = []
    for tag in ("cam0", "cam1", "cam2", "cam3", "imu"):
        nested = os.path.join(base, tag, f"{tag}_0.mcap")
        flat = os.path.join(base, f"{tag}_0.mcap")
        path = nested if os.path.exists(nested) else flat
        sources.append((tag, path))
    return sources


def _convert(sources, out_bag, queue_size=QUEUE_SIZE, remap_topics=False):
    ts1 = get_typestore(Stores.ROS1_NOETIC)

    if os.path.exists(out_bag):
        os.remove(out_bag)

    active_sources = [
        (idx, tag, path)
        for idx, (tag, path) in enumerate(sources)
        if os.path.exists(path)
    ]
    for _, tag, path in active_sources:
        print(f"[读取] {tag}: {path}")
    for tag, path in sources:
        if not os.path.exists(path):
            print(f"[跳过] 文件不存在: {path}")

    if not active_sources:
        print("[ERROR] 没有找到任何 mcap 输入文件")
        return

    queues = {idx: queue.Queue(maxsize=queue_size) for idx, _, _ in active_sources}
    threads = []
    for idx, tag, path in active_sources:
        thread = threading.Thread(
            target=_convert_source_worker,
            args=(idx, tag, path, queues[idx], remap_topics),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    total = 0
    topic_stats = {}
    bag_t_min = None
    bag_t_max = None
    source_counts = {}
    heap = []
    heap_counter = itertools.count()

    with Writer(out_bag) as writer:
        conn_cache = {}
        for idx, _, _ in active_sources:
            _push_next_from_source(idx, queues[idx], heap, source_counts, heap_counter)

        while heap:
            _, _, source_idx, msg = heapq.heappop(heap)

            key = (msg.topic, msg.typename)
            conn = conn_cache.get(key)
            if conn is None:
                conn = writer.add_connection(
                    msg.topic,
                    msg.typename,
                    typestore=ts1,
                    callerid=CALLERIDS.get(msg.topic),
                    latching=0,
                )
                conn_cache[key] = conn

            writer.write(conn, msg.ts, msg.raw)
            total += 1

            st = topic_stats.get(msg.topic)
            if st is None:
                st = {"type": msg.typename, "count": 0, "t_min": msg.ts, "t_max": msg.ts}
                topic_stats[msg.topic] = st
            st["count"] += 1
            if msg.ts < st["t_min"]:
                st["t_min"] = msg.ts
            if msg.ts > st["t_max"]:
                st["t_max"] = msg.ts
            if bag_t_min is None or msg.ts < bag_t_min:
                bag_t_min = msg.ts
            if bag_t_max is None or msg.ts > bag_t_max:
                bag_t_max = msg.ts

            if total % 5000 == 0:
                print(f"    已写入: {total} 条...")

            _push_next_from_source(source_idx, queues[source_idx], heap, source_counts, heap_counter)

    for thread in threads:
        thread.join()

    for idx, tag, _ in active_sources:
        print(f"[完成] {tag}: 共 {source_counts.get(idx, 0)} 条")

    print(f"\n全部完成,总计 {total} 条消息 -> {out_bag}")
    _print_topic_stats(topic_stats, bag_t_min, bag_t_max)


def _push_next_from_source(source_idx, source_queue, heap, source_counts, heap_counter):
    item = source_queue.get()
    if isinstance(item, SourceDone):
        source_counts[source_idx] = item.count
        if item.error is not None:
            raise item.error
        return
    heapq.heappush(heap, (item.ts, next(heap_counter), source_idx, item))


def _convert_source_worker(source_idx, tag, path, out_queue, remap_topics):
    ts2 = get_typestore(Stores.ROS2_HUMBLE)
    ts1 = get_typestore(Stores.ROS1_NOETIC)
    schemas = {}
    channels = {}
    topic_seq = {}
    count = 0

    try:
        with open(path, "rb") as f:
            sr = StreamReader(f, emit_chunks=False)
            rec_iter = iter(sr.records)
            while True:
                try:
                    rec = next(rec_iter)
                except StopIteration:
                    break
                except Exception as e:
                    print(f"    [提示] {tag} 末尾数据截断,已停止读取该文件: {type(e).__name__}: {e}")
                    break

                if isinstance(rec, Schema):
                    schemas[rec.id] = rec
                elif isinstance(rec, Channel):
                    channels[rec.id] = rec
                elif isinstance(rec, Message):
                    ch = channels[rec.channel_id]
                    sc = schemas[ch.schema_id]
                    typename = sc.name
                    src_topic = ch.topic
                    if remap_topics:
                        topic = LEGACY_TOPIC_REMAP.get(src_topic, src_topic)
                    else:
                        topic = KALIBR_TOPIC_REMAP.get(src_topic, src_topic)

                    msg = ts2.deserialize_cdr(rec.data, typename)
                    hdr = getattr(msg, "header", None)
                    if hdr is not None:
                        seq = topic_seq.get(topic, 0)
                        hdr.seq = seq
                        topic_seq[topic] = seq + 1

                    raw = ts1.serialize_ros1(msg, typename)
                    out_queue.put(ConvertedMessage(rec.log_time, topic, typename, raw, source_idx))
                    count += 1
                    if count % 5000 == 0:
                        print(f"    {tag}: 已转换 {count} 条...")
    except Exception as e:
        out_queue.put(SourceDone(count, e))
    else:
        out_queue.put(SourceDone(count))


def _print_topic_stats(topic_stats, bag_t_min, bag_t_max):
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
