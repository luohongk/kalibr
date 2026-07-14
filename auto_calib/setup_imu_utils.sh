#!/bin/bash
# setup_imu_utils.sh  ——  (容器内) 确保 imu_utils 已编译可用
# 幂等: 已编译则直接退出; 否则装 Ceres 并 catkin build。
set -eo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash 2>/dev/null || true

# PyAV (步骤2 转换 H.264 相机流用). 幂等: 已装则跳过。
# 注意: 此处独立检查, 必须在下方 imu_an 早退分支之前, 否则 imu_an 已存在时不会执行。
# 该 PyPI 只提供 av 12.x (需 py3.9+), py3.8 装不了 wheel -> 源码编译 av 10.0.0 (兼容 ffmpeg 4.2)。
if ! python3 -c 'import av' 2>/dev/null; then
    echo "[setup] 安装 PyAV (源码编译 av==10.0.0)..."
    python3 -m pip install -U pip >/dev/null 2>&1 || true
    python3 -m pip install setuptools wheel >/dev/null 2>&1 || true
    python3 -m pip install --no-build-isolation --no-binary av 'av==10.0.0' \
        || { echo "[setup] PyAV 安装失败"; exit 1; }
    echo "[setup] PyAV 就绪"
fi

if [ -x /catkin_ws/devel/lib/imu_utils/imu_an ] || \
   [ -x /catkin_ws/devel/.private/imu_utils/lib/imu_utils/imu_an ]; then
    echo "[setup] imu_utils 已就绪 (imu_an 二进制存在)"
    exit 0
fi

echo "[setup] 安装依赖 (Ceres/libdw)..."
apt-get update >/dev/null 2>&1 || true
apt-get install -y libceres-dev libdw-dev >/dev/null 2>&1 || true

# src/imu_utils 存在但为空(无 package.xml) 会导致 catkin build 报 "not in the workspace"。
# 这种情况按未克隆处理: 清掉空目录重新 clone。
if [ -d /catkin_ws/src/imu_utils ] && [ ! -f /catkin_ws/src/imu_utils/package.xml ]; then
    echo "[setup] src/imu_utils 为空, 重新 clone..."
    rm -rf /catkin_ws/src/imu_utils
fi
if [ ! -d /catkin_ws/src/imu_utils ]; then
    echo "[setup] clone imu_utils..."
    git clone --depth 1 https://github.com/luohongk/imu_utils.git /catkin_ws/src/imu_utils
fi

echo "[setup] catkin build imu_utils..."
cd /catkin_ws
catkin build imu_utils -DCMAKE_BUILD_TYPE=Release
source /catkin_ws/devel/setup.bash

[ -x /catkin_ws/devel/lib/imu_utils/imu_an ] && echo "[setup] imu_utils 编译完成" || { echo "[setup] 失败"; exit 1; }
