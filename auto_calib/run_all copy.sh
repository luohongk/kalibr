#!/bin/bash
# run_all.sh  ——  宿主机批量驱动: 遍历 DATA_ROOT 下所有数据文件夹, 逐个在容器内标定
#
# 用法 (宿主机):
#   bash run_all.sh                      # 处理 DATA_ROOT 下所有含 calibration.bag 的文件夹
#   bash run_all.sh 111 222              # 只处理指定文件夹
#   CAM_HZ=10 CAM_BAG_FREQ=3 IMUCAM_BAG_FREQ=30 IMU_SAFETY=5 bash run_all.sh
#
# 约定: 宿主机 DATA_ROOT 已挂载到容器 /data; 仓库根挂到 /catkin_ws/src/kalibr_local
set -uo pipefail

DATA_ROOT="${DATA_ROOT:-/home/conanluo/kalibr_data}"
CONTAINER="${CONTAINER:-kalibr_work}"
DATA_MNT="${DATA_MNT:-/data}"        # 容器内 DATA_ROOT 的挂载点

# 透传给 run_one.sh 的可调参数
export_vars="CAM_HZ BAG_FREQ CAM_BAG_FREQ IMUCAM_BAG_FREQ IMU_RATE IMU_SAFETY MODELS TARGET_NAME TARGET_SRC PLAY_RATE KEEP_CONVERTED REPROJ_WARN"

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
        [ -f "${d}calibration.bag" ] && FOLDERS+=("$(basename "$d")")
    done
fi

if [ "${#FOLDERS[@]}" -eq 0 ]; then
    echo "[ERROR] 在 $DATA_ROOT 下没找到含 calibration.bag 的文件夹" >&2
    exit 1
fi

echo "==================================================================="
echo " 批量标定  共 ${#FOLDERS[@]} 个文件夹: ${FOLDERS[*]}"
echo " 容器: $CONTAINER   数据根(容器内): $DATA_MNT"
echo "==================================================================="

# 把最新脚本同步进容器 (bind mount 在本环境不可靠, 用 docker cp)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
docker exec "$CONTAINER" mkdir -p /opt/auto_calib
docker cp "$SCRIPT_DIR/." "$CONTAINER:/opt/auto_calib/" >/dev/null

# 把标定板配置一并拷进容器 /opt/auto_calib/ (run_one.sh 默认从这里取)
# 标定板位于仓库根的 calibration_board_data/, 与挂载点无关, 拷贝最可靠。
if [ -d "$REPO_ROOT/calibration_board_data" ]; then
    docker cp "$REPO_ROOT/calibration_board_data/." "$CONTAINER:/opt/auto_calib/" >/dev/null
else
    echo "[WARN] 未找到 $REPO_ROOT/calibration_board_data, 标定板需由容器内 TARGET_SRC 指定" >&2
fi

docker exec "$CONTAINER" bash /opt/auto_calib/setup_imu_utils.sh

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
