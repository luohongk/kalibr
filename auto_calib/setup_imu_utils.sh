#!/bin/bash
# setup_imu_utils.sh  ——  (容器内) 确保 imu_utils 已编译可用
# 幂等: 已编译则直接退出; 否则装 Ceres 并 catkin build。
set -eo pipefail

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash 2>/dev/null || true

if rospack find imu_utils >/dev/null 2>&1 && \
   [ -x /catkin_ws/devel/lib/imu_utils/imu_an ]; then
    echo "[setup] imu_utils 已就绪"
    exit 0
fi

echo "[setup] 安装依赖 (Ceres/libdw)..."
apt-get update >/dev/null 2>&1 || true
apt-get install -y libceres-dev libdw-dev >/dev/null 2>&1 || true

if [ ! -d /catkin_ws/src/imu_utils ]; then
    echo "[setup] clone imu_utils..."
    git clone --depth 1 https://github.com/luohongk/imu_utils.git /catkin_ws/src/imu_utils
fi

echo "[setup] catkin build imu_utils..."
cd /catkin_ws
catkin build imu_utils -DCMAKE_BUILD_TYPE=Release
source /catkin_ws/devel/setup.bash

[ -x /catkin_ws/devel/lib/imu_utils/imu_an ] && echo "[setup] imu_utils 编译完成" || { echo "[setup] 失败"; exit 1; }
