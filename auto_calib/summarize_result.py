#!/usr/bin/env python3
"""
summarize_result.py

解析 Kalibr 两个标定结果 txt, 汇总关键指标并给出好坏判定:
  *-results-cam.txt      (kalibr_calibrate_cameras 输出)
  *-results-imucam.txt   (kalibr_calibrate_imu_camera 输出)

提取:
  - 每路相机的重投影误差 (px)
  - cam-imu 联合标定的重投影误差 / 陀螺/加速度残差
  - 每路 timeshift cam->imu (s)

判定:
  - 任一相机重投影误差 mean > REPROJ_WARN(默认 1.0 px) -> 标 [WARN]
  - 缺少关键文件 -> 标 [FAIL]
退出码: 0=全部通过, 2=有 WARN, 3=有 FAIL/缺文件。
(退出码仅供参考, run_one.sh 不因 WARN 中断, 只记录到 summary.txt)

用法:
  python3 summarize_result.py --dir <result_dir> [--reproj-warn 1.0] [--out summary.txt]
"""
import argparse
import glob
import os
import re
import sys


def find_one(d, pattern):
    hits = sorted(glob.glob(os.path.join(d, pattern)))
    return hits[0] if hits else None


def parse_cam_result(path):
    """返回 {cam_idx: {'topic':..., 'reproj_mean':(x,y), 'reproj_std':(x,y)}} 以及 baselines 文本块。"""
    cams = {}
    if not path or not os.path.isfile(path):
        return cams
    cur = None
    with open(path, "r") as f:
        for line in f:
            m = re.match(r"\s*cam(\d+)\s*\(([^)]*)\):", line)
            if m:
                cur = int(m.group(1))
                cams[cur] = {"topic": m.group(2).strip()}
                continue
            m = re.search(
                r"reprojection error:\s*\[([-\d.eE]+),\s*([-\d.eE]+)\]"
                r"\s*\+-\s*\[([-\d.eE]+),\s*([-\d.eE]+)\]", line)
            if m and cur is not None:
                cams[cur]["reproj_mean"] = (float(m.group(1)), float(m.group(2)))
                cams[cur]["reproj_std"] = (float(m.group(3)), float(m.group(4)))
    return cams


def parse_imucam_result(path):
    """返回 dict: reproj[cam], gyro, accel, timeshift[cam]。"""
    out = {"reproj": {}, "gyro": None, "accel": None, "timeshift": {}}
    if not path or not os.path.isfile(path):
        return out
    lines = open(path, "r").read().splitlines()
    for i, line in enumerate(lines):
        m = re.search(r"Reprojection error \(cam(\d+)\)[^:]*:\s*mean\s*([-\d.eE]+),"
                      r"\s*median\s*([-\d.eE]+),\s*std:\s*([-\d.eE]+)", line)
        if m:
            out["reproj"][int(m.group(1))] = {
                "mean": float(m.group(2)), "median": float(m.group(3)), "std": float(m.group(4))}
            continue
        m = re.search(r"Gyroscope error \(imu\d+\)[^:]*:\s*mean\s*([-\d.eE]+)", line)
        if m:
            out["gyro"] = float(m.group(1)); continue
        m = re.search(r"Accelerometer error \(imu\d+\)[^:]*:\s*mean\s*([-\d.eE]+)", line)
        if m:
            out["accel"] = float(m.group(1)); continue
        # timeshift camN to imu0: [s] ...  下一行是数值
        m = re.match(r"\s*timeshift cam(\d+) to imu0:", line)
        if m and i + 1 < len(lines):
            try:
                out["timeshift"][int(m.group(1))] = float(lines[i + 1].strip())
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="结果目录 (含 *-results-*.txt)")
    ap.add_argument("--reproj-warn", type=float,
                    default=float(os.environ.get("REPROJ_WARN", "1.0")),
                    help="相机重投影误差(mean,px)告警阈值, 默认1.0")
    ap.add_argument("--out", default=None, help="汇总输出文件 (默认 <dir>/summary.txt)")
    args = ap.parse_args()

    d = args.dir
    out_path = args.out or os.path.join(d, "summary.txt")
    cam_txt = find_one(d, "*-results-cam.txt")
    imucam_txt = find_one(d, "*-results-imucam.txt")

    cams = parse_cam_result(cam_txt)
    ic = parse_imucam_result(imucam_txt)

    lines = []
    status = "OK"   # OK / WARN / FAIL

    lines.append("=" * 60)
    lines.append(" Kalibr 标定结果汇总")
    lines.append("=" * 60)

    # ---- 相机内参标定 ----
    lines.append("")
    lines.append("[1] 多目相机标定  (%s)" % (os.path.basename(cam_txt) if cam_txt else "缺失"))
    if not cams:
        lines.append("    [FAIL] 未解析到相机结果")
        status = "FAIL"
    else:
        for idx in sorted(cams):
            c = cams[idx]
            rm = c.get("reproj_mean")
            if rm:
                mag = (rm[0] ** 2 + rm[1] ** 2) ** 0.5
                flag = "WARN" if mag > args.reproj_warn else "ok"
                if flag == "WARN" and status == "OK":
                    status = "WARN"
                lines.append("    cam%d %-22s reproj mean=[%.3f, %.3f] |%.3f|px  [%s]"
                             % (idx, "(%s)" % c.get("topic", "?"), rm[0], rm[1], mag, flag))
            else:
                lines.append("    cam%d %-22s 无重投影误差(观测不足?)  [WARN]"
                             % (idx, "(%s)" % c.get("topic", "?")))
                if status == "OK":
                    status = "WARN"

    # ---- Camera-IMU 联合标定 ----
    lines.append("")
    lines.append("[2] Camera-IMU 联合标定  (%s)"
                 % (os.path.basename(imucam_txt) if imucam_txt else "缺失"))
    if not imucam_txt:
        lines.append("    [FAIL] 缺少 *-results-imucam.txt")
        status = "FAIL"
    else:
        if ic["reproj"]:
            for idx in sorted(ic["reproj"]):
                r = ic["reproj"][idx]
                flag = "WARN" if r["mean"] > args.reproj_warn else "ok"
                if flag == "WARN" and status == "OK":
                    status = "WARN"
                lines.append("    cam%d reproj  mean=%.3f median=%.3f std=%.3f px  [%s]"
                             % (idx, r["mean"], r["median"], r["std"], flag))
        else:
            lines.append("    [WARN] 未解析到联合标定重投影误差")
            if status == "OK":
                status = "WARN"
        if ic["gyro"] is not None:
            lines.append("    gyro  error mean = %.5f rad/s" % ic["gyro"])
        if ic["accel"] is not None:
            lines.append("    accel error mean = %.5f m/s^2" % ic["accel"])
        if ic["timeshift"]:
            for idx in sorted(ic["timeshift"]):
                lines.append("    timeshift cam%d->imu0 = %+.6f s" % (idx, ic["timeshift"][idx]))

    lines.append("")
    lines.append("-" * 60)
    lines.append(" 总判定: [%s]   (相机重投影告警阈值 %.2f px)" % (status, args.reproj_warn))
    lines.append("=" * 60)

    text = "\n".join(lines) + "\n"
    print(text)
    try:
        with open(out_path, "w") as f:
            f.write(text)
        print("[summary] 已写入 %s" % out_path)
    except OSError as e:
        print("[summary] 写入失败 %s: %s" % (out_path, e))

    sys.exit({"OK": 0, "WARN": 2, "FAIL": 3}[status])


if __name__ == "__main__":
    main()
