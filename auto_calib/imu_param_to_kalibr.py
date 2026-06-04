#!/usr/bin/env python3
"""
imu_param_to_kalibr.py

把 imu_utils 输出的 <name>_imu_param.yaml (Allan 方差结果)
转换成 Kalibr 需要的 imu.yaml 格式。

imu_utils 输出 (关心 avg-axis):
  Gyr.avg-axis.gyr_n  -> gyroscope_noise_density   (rad/s/sqrt(Hz))
  Gyr.avg-axis.gyr_w  -> gyroscope_random_walk     (rad/s^2/sqrt(Hz))
  Acc.avg-axis.acc_n  -> accelerometer_noise_density (m/s^2/sqrt(Hz))
  Acc.avg-axis.acc_w  -> accelerometer_random_walk   (m/s^3/sqrt(Hz))

用法:
  python3 imu_param_to_kalibr.py --in mydata_imu_param.yaml --out imu.yaml \
      --rate 200 [--safety 1.0]
注: imu_utils 的标定结果通常偏乐观, 工程上常把 noise/random_walk 乘一个安全系数
    (5~10)。这里默认 safety=1.0, 可按需调大。
"""
import argparse
import sys

# imu_utils 的 yaml 头部是 "%YAML:1.0" (OpenCV 风格), 标准 PyYAML 解析会报错。
# 这里做轻量手工解析, 只抓我们需要的 4 个 avg-axis 值。


def parse_imu_utils_yaml(path):
    vals = {}
    section = None      # "Gyr" / "Acc"
    in_avg = False
    with open(path, "r") as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("%"):
                continue
            indent = len(line) - len(line.lstrip())
            if stripped.startswith("Gyr:"):
                section, in_avg = "Gyr", False
                continue
            if stripped.startswith("Acc:"):
                section, in_avg = "Acc", False
                continue
            if stripped.startswith("avg-axis:"):
                in_avg = True
                continue
            # x-axis/y-axis/z-axis 重置 avg 标记
            if stripped.startswith(("x-axis:", "y-axis:", "z-axis:")):
                in_avg = False
                continue
            if in_avg and ":" in stripped:
                key, _, v = stripped.partition(":")
                key = key.strip()
                try:
                    vals[key] = float(v.strip())
                except ValueError:
                    pass
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--safety", type=float, default=1.0,
                    help="noise/random_walk 安全放大系数 (默认1.0)")
    args = ap.parse_args()

    v = parse_imu_utils_yaml(args.inp)
    need = ["gyr_n", "gyr_w", "acc_n", "acc_w"]
    missing = [k for k in need if k not in v]
    if missing:
        print("[ERROR] %s 缺少字段: %s (解析到: %s)" % (args.inp, missing, v))
        sys.exit(2)

    s = args.safety
    acc_n = v["acc_n"] * s
    acc_w = v["acc_w"] * s
    gyr_n = v["gyr_n"] * s
    gyr_w = v["gyr_w"] * s

    content = (
        "#Accelerometers\n"
        "accelerometer_noise_density: %.10e   #Noise density (continuous-time)\n"
        "accelerometer_random_walk:   %.10e   #Bias random walk\n\n"
        "#Gyroscopes\n"
        "gyroscope_noise_density:     %.10e   #Noise density (continuous-time)\n"
        "gyroscope_random_walk:       %.10e   #Bias random walk\n\n"
        "rostopic:                    /imu0      #the IMU ROS topic\n"
        "update_rate:                 %.1f       #Hz (for discretization)\n"
    ) % (acc_n, acc_w, gyr_n, gyr_w, args.rate)

    with open(args.out, "w") as f:
        f.write(content)
    print("[imu.yaml] written -> %s (safety=%.1f, rate=%.1f)" % (args.out, s, args.rate))
    print(content)


if __name__ == "__main__":
    main()
