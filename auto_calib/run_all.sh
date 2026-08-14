#!/bin/bash
# run_all.sh  ——  宿主机批量驱动: 遍历 DATA_ROOT 下所有数据文件夹, 逐个在容器内标定
#
# 用法 (宿主机):
#   bash run_all.sh                      # 处理 DATA_ROOT 下所有含三个必需 bag 的文件夹
#   bash run_all.sh 111 222              # 只处理指定文件夹
#   CAM_BAG_FREQ=1 IMUCAM_BAG_FREQ=30 IMU_SAFETY=5 bash run_all.sh
#
# 每个数据目录必须包含:
#   calibration_4cam.bag calibration_cam0_imu.bag imu.bag
#
# 约定: 宿主机 DATA_ROOT 已挂载到容器 /data; 仓库根挂到 /catkin_ws/src/kalibr_local
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/home/conanluo/kalibr_data}"
CONTAINER="${CONTAINER:-kalibr_work}"
DATA_MNT="${DATA_MNT:-/data}"        # 容器内 DATA_ROOT 的挂载点

# 透传给 run_one.sh 的可调参数; BAG_FREQ 保留为两阶段统一覆盖的兼容入口
export_vars="CAM_HZ CAM_CONVERT_HZ IMUCAM_CONVERT_HZ BAG_FREQ CAM_BAG_FREQ IMUCAM_BAG_FREQ IMU_RATE IMU_SAFETY MODELS CAM_FREEZE_INTRINSICS_RMSE CAM_FREEZE_INTRINSICS_MIN_VIEWS CAM_FREEZE_INTRINSICS_STABLE_VIEWS CAM_USE_BLAKE_ZISSERMAN CAM_NO_SHUFFLE KALIBR_EXTRACT_JOBS KALIBR_OPT_THREADS TARGET_NAME TARGET_SRC PLAY_RATE KEEP_CONVERTED REPROJ_WARN"

# 确认容器在跑
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[ERROR] 容器 $CONTAINER 未运行。先启动它 (docker start $CONTAINER 或 docker run ...)" >&2
    exit 1
fi

# 选定要处理的文件夹
if [ "$#" -gt 0 ]; then
    FOLDERS=("$@")
else
    FOLDERS=()
    for d in "$DATA_ROOT"/*/; do
        if [ -f "${d}calibration_4cam.bag" ] && \
           [ -f "${d}calibration_cam0_imu.bag" ] && \
           [ -f "${d}imu.bag" ]; then
            FOLDERS+=("$(basename "$d")")
        fi
    done
fi

if [ "${#FOLDERS[@]}" -eq 0 ]; then
    echo "[ERROR] 在 $DATA_ROOT 下没找到同时包含 calibration_4cam.bag、calibration_cam0_imu.bag 和 imu.bag 的文件夹" >&2
    exit 1
fi

echo "==================================================================="
echo " 批量标定  共 ${#FOLDERS[@]} 个文件夹: ${FOLDERS[*]}"
echo " 容器: $CONTAINER   数据根(容器内): $DATA_MNT"
echo "==================================================================="

# 把最新脚本同步进容器 (bind mount 在本环境不可靠, 用 docker cp)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
docker exec "$CONTAINER" mkdir -p /opt/auto_calib || {
    echo "[ERROR] 创建容器脚本目录失败" >&2; exit 2; }
docker cp "$SCRIPT_DIR/." "$CONTAINER:/opt/auto_calib/" >/dev/null || {
    echo "[ERROR] 同步标定脚本失败" >&2; exit 2; }

# kalibr_calibrate_cameras is a catkin relay that reads these Python sources
# directly. Sync the two-stage intrinsics-freeze implementation as well; no
# rebuild is needed for Python-only changes.
KALIBR_PY_REL="aslam_offline_calibration/kalibr/python"
docker cp "$REPO_ROOT/$KALIBR_PY_REL/kalibr_calibrate_cameras" \
    "$CONTAINER:/catkin_ws/src/kalibr/$KALIBR_PY_REL/kalibr_calibrate_cameras" >/dev/null && \
docker cp "$REPO_ROOT/$KALIBR_PY_REL/kalibr_camera_calibration/CameraCalibrator.py" \
    "$CONTAINER:/catkin_ws/src/kalibr/$KALIBR_PY_REL/kalibr_camera_calibration/CameraCalibrator.py" >/dev/null && \
docker cp "$REPO_ROOT/$KALIBR_PY_REL/kalibr_camera_calibration/CameraUtils.py" \
    "$CONTAINER:/catkin_ws/src/kalibr/$KALIBR_PY_REL/kalibr_camera_calibration/CameraUtils.py" >/dev/null && \
docker cp "$REPO_ROOT/$KALIBR_PY_REL/kalibr_common/TargetExtractor.py" \
    "$CONTAINER:/catkin_ws/src/kalibr/$KALIBR_PY_REL/kalibr_common/TargetExtractor.py" >/dev/null || {
    echo "[ERROR] 同步 Kalibr Python 实现失败" >&2; exit 2; }

# 把标定板配置一并拷进容器 /opt/auto_calib/ (run_one.sh 默认从这里取)
# 标定板位于仓库根的 calibration_board_data/, 与挂载点无关, 拷贝最可靠。
if [ -d "$REPO_ROOT/calibration_board_data" ]; then
    docker cp "$REPO_ROOT/calibration_board_data/." "$CONTAINER:/opt/auto_calib/" >/dev/null || {
        echo "[ERROR] 同步标定板配置失败" >&2; exit 2; }
else
    echo "[WARN] 未找到 $REPO_ROOT/calibration_board_data, 标定板需由容器内 TARGET_SRC 指定" >&2
fi

docker exec "$CONTAINER" bash /opt/auto_calib/setup_imu_utils.sh || {
    echo "[ERROR] 初始化 imu_utils 失败" >&2; exit 3; }

# 组装要透传的环境变量
ENVARGS=()
for v in $export_vars; do
    if [ -n "${!v:-}" ]; then ENVARGS+=("-e" "$v=${!v}"); fi
done

OK=(); FAIL=()
for name in "${FOLDERS[@]}"; do
    echo ""
    echo "######### >>> $name <<< #########"
    if docker exec "${ENVARGS[@]}" "$CONTAINER" \
            bash /opt/auto_calib/run_one.sh "$DATA_MNT/$name"; then
        OK+=("$name")
    else
        FAIL+=("$name")
        echo "[WARN] $name 失败, 继续下一个"
    fi
done

echo ""
echo "==================================================================="
echo " 完成。成功 ${#OK[@]}: ${OK[*]:-无}"
echo " 失败 ${#FAIL[@]}: ${FAIL[*]:-无}"
echo " 结果在各文件夹的 result/ 子目录"
echo "==================================================================="
[ "${#FAIL[@]}" -eq 0 ]
