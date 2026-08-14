#!/bin/bash
# run_one.sh  ——  在 Docker 容器内对单个数据文件夹做完整标定
# 用法 (容器内):  bash run_one.sh /data/111
#
# 重要: /data 是 goosefs 对象存储 FUSE, 只支持"整文件写入(cp)", 不支持 append/重写。
#       因此所有中间计算都在容器本地 /work 下完成, 最后把结果 cp 回 /data/<folder>/result/。
#
# 期望输入:
#   <folder>/calibration_4cam.bag      (4 路相机内外参)
#   <folder>/calibration_cam0_imu.bag  (cam0 + IMU 联合标定)
#   <folder>/imu.bag                   (IMU Allan 方差)
# 产出 (cp 到 <folder>/result/):
#   imu.yaml
#   kalibr_input-camchain.yaml           (相机内外参)
#   kalibr_input-camchain-imucam.yaml    (cam-imu 外参, 最终结果)
#   <name>_imu_param.yaml, *.log, *.pdf  (过程文件)
set -o pipefail

DATA_FOLDER="${1:?用法: run_one.sh /data/<folder>}"
LEGACY_BAG_FREQ_USED=0
if [ -n "${BAG_FREQ:-}" ] && { [ -z "${CAM_BAG_FREQ:-}" ] || [ -z "${IMUCAM_BAG_FREQ:-}" ]; }; then
    LEGACY_BAG_FREQ_USED=1
fi
CAM_BAG_FREQ="${CAM_BAG_FREQ:-${BAG_FREQ:-1}}"          # 步骤3相机标定处理频率; BAG_FREQ 为兼容兜底
IMUCAM_BAG_FREQ="${IMUCAM_BAG_FREQ:-${BAG_FREQ:-30}}"   # 步骤4相机-IMU标定处理频率; BAG_FREQ 为兼容兜底
# CAM_HZ 是旧版“两个 bag 共用转换频率”的覆盖项。默认分别按下游实际
# 处理频率转换，避免先解码/写全帧，随后又被 Kalibr 的 --bag-freq 丢掉。
CAM_HZ="${CAM_HZ:-}"
CAM_CONVERT_HZ="${CAM_CONVERT_HZ:-${CAM_HZ:-$CAM_BAG_FREQ}}"
IMUCAM_CONVERT_HZ="${IMUCAM_CONVERT_HZ:-${CAM_HZ:-$IMUCAM_BAG_FREQ}}"
IMU_RATE="${IMU_RATE:-200}"
IMU_TOPIC="${IMU_TOPIC:-/imu_data_raw}"   # imu.bag 里的 IMU 话题名 (rosbag info 确认)
IMU_SAFETY="${IMU_SAFETY:-1.0}"
MODELS="${MODELS:-eucm-none eucm-none eucm-none eucm-none}"
CAM_FREEZE_INTRINSICS_RMSE="${CAM_FREEZE_INTRINSICS_RMSE:-0.2}"
CAM_FREEZE_INTRINSICS_MIN_VIEWS="${CAM_FREEZE_INTRINSICS_MIN_VIEWS:-30}"
CAM_FREEZE_INTRINSICS_STABLE_VIEWS="${CAM_FREEZE_INTRINSICS_STABLE_VIEWS:-5}"
CAM_USE_BLAKE_ZISSERMAN="${CAM_USE_BLAKE_ZISSERMAN:-1}"
CAM_NO_SHUFFLE="${CAM_NO_SHUFFLE:-0}"
KALIBR_EXTRACT_JOBS="${KALIBR_EXTRACT_JOBS:-16}"
KALIBR_OPT_THREADS="${KALIBR_OPT_THREADS:-16}"
export KALIBR_EXTRACT_JOBS KALIBR_OPT_THREADS
# MODELS="${MODELS:-pinhole-equi pinhole-equi pinhole-equi pinhole-equi}"
# MODELS="${MODELS:-omni-radtan omni-radtan omni-radtan omni-radtan}"
# 标定板文件名 (checkerboard.yaml / aprilgrid.yaml)
TARGET_NAME="${TARGET_NAME:-checkerboard.yaml}"
# TARGET_SRC 可显式指定标定板路径; 留空则在多个候选位置自动查找
TARGET_SRC="${TARGET_SRC:-}"
PLAY_RATE="${PLAY_RATE:-50}"
SCRIPTS="${SCRIPTS:-/opt/auto_calib}"
KEEP_CONVERTED="${KEEP_CONVERTED:-0}"

NAME="$(basename "$DATA_FOLDER")"
CAL_4CAM_BAG="$DATA_FOLDER/calibration_4cam.bag"
CAL_IMUCAM_BAG="$DATA_FOLDER/calibration_cam0_imu.bag"
IMU_BAG="$DATA_FOLDER/imu.bag"
RESULT_DST="$DATA_FOLDER/result"      # 最终结果落地 (goosefs, 整文件 cp)

WORK="/work/$NAME"                    # 容器本地工作区
OUTDIR="$WORK/out"                    # 本地输出, 跑完整体 cp 回 RESULT_DST
CAM_WORK="$WORK/cam"
IMUCAM_WORK="$WORK/imucam"
BOARD_WORK="$WORK/board"
CAM_CONV_BAG="$CAM_WORK/kalibr_input.bag"
IMUCAM_CONV_BAG="$IMUCAM_WORK/kalibr_input.bag"

log(){ echo -e "\033[1;36m[$(date +%H:%M:%S)][$NAME] $*\033[0m"; }
err(){ echo -e "\033[1;31m[$(date +%H:%M:%S)][$NAME][ERR] $*\033[0m"; }

# 同一数据集的两个任务会共享 /work/$NAME。若重叠运行，任一任务结尾的
# rm -rf 会删除另一个任务正在使用的 cam/out 目录。锁文件放在 /tmp，
# 不会被工作目录清理影响；不同数据集使用不同锁，仍可并行。
LOCK_KEY="$(printf '%s' "$NAME" | tr -c 'A-Za-z0-9_.-' '_')"
LOCK_FILE="/tmp/auto_calib_${LOCK_KEY}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    err "同一数据集已有标定任务运行中；为避免互删 $WORK，本次拒绝启动"
    exit 13
fi

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-localhost}"
export PYTHONUNBUFFERED=1          # 让 kalibr 的进度条实时刷新, 不被行缓冲卡住
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash

[ -f "$CAL_4CAM_BAG" ] || { err "缺少 $CAL_4CAM_BAG"; exit 10; }
[ -f "$CAL_IMUCAM_BAG" ] || { err "缺少 $CAL_IMUCAM_BAG"; exit 11; }
[ -f "$IMU_BAG" ] || { err "缺少 $IMU_BAG"; exit 12; }

log "输入: 4cam=$CAL_4CAM_BAG"
log "输入: cam0-imu=$CAL_IMUCAM_BAG"
log "输入: Allan IMU=$IMU_BAG"
log "频率: 4cam 转换/标定=${CAM_CONVERT_HZ}/${CAM_BAG_FREQ}Hz, cam0-IMU 转换/标定=${IMUCAM_CONVERT_HZ}/${IMUCAM_BAG_FREQ}Hz"
[ "$LEGACY_BAG_FREQ_USED" -eq 0 ] || log "[WARN] BAG_FREQ 为兼容变量; 建议改用 CAM_BAG_FREQ 和 IMUCAM_BAG_FREQ"

# 完成标记 (在 goosefs 上, 整文件 cp 写入)
if [ -f "$RESULT_DST/.done" ]; then
    log "已存在 result/.done, 跳过 (删除可重跑)"
    exit 0
fi

rm -rf "$WORK" || { err "清理旧工作区失败: $WORK"; exit 14; }
mkdir -p "$OUTDIR" "$CAM_WORK" "$IMUCAM_WORK" "$BOARD_WORK" || {
    err "创建本地工作目录失败: $WORK"; exit 14; }
# 准备标定板配置到本地: 显式 TARGET_SRC 优先, 否则按候选位置查找 TARGET_NAME。
# 候选顺序: run_all.sh 拷进来的 /opt/auto_calib -> 仓库挂载点 -> /data 数据根。
if [ -z "$TARGET_SRC" ]; then
    for cand in \
        "$SCRIPTS/$TARGET_NAME" \
        "/catkin_ws/src/kalibr/calibration_board_data/$TARGET_NAME" \
        "/data/calibration_board_data/$TARGET_NAME"; do
        [ -f "$cand" ] && { TARGET_SRC="$cand"; break; }
    done
fi
[ -n "$TARGET_SRC" ] && [ -f "$TARGET_SRC" ] || {
    err "找不到标定板 $TARGET_NAME (候选: $SCRIPTS, 仓库 calibration_board_data, /data/calibration_board_data; 或用 TARGET_SRC 指定)"; exit 12; }
cp "$TARGET_SRC" "$BOARD_WORK/" || { err "拷贝标定板失败: $TARGET_SRC"; exit 12; }
TARGET="$BOARD_WORK/$(basename "$TARGET_SRC")"
log "标定板: $TARGET_SRC"

############################################
# 步骤 1: IMU 内参 (imu_utils Allan 方差)
############################################
# 注意: imu_name 必须是非纯数字字符串, 否则 ROS 会按 int 解析导致 readParam<string> 取到空。
IMU_NAME="imu_${NAME}"
case "$IMU_NAME" in ''|*[!a-zA-Z]*) ;; esac   # 保险: 含字母即可
IMU_PARAM="$OUTDIR/${IMU_NAME}_imu_param.yaml"
IMU_YAML="$OUTDIR/imu.yaml"
log "步骤1: imu_utils Allan 方差标定"
DUR=$(python3 -c "import rosbag; b=rosbag.Bag('$IMU_BAG'); print(b.get_end_time()-b.get_start_time())" 2>/dev/null)
# max_time_min 固定 12 (对齐 imu_utils 官方 mydata.launch); 可用 MAX_TIME_MIN 覆盖。
MAXMIN="${MAX_TIME_MIN:-12}"
log "  imu.bag 时长≈${DUR}s -> max_time_min=${MAXMIN}"

roscore >"$OUTDIR/roscore.log" 2>&1 &
ROSCORE_PID=$!
for i in $(seq 1 40); do rostopic list >/dev/null 2>&1 && break; sleep 0.3; done

# imu_utils 在 data_save_path 下写多个文件 -> 用本地目录 (末尾带 /)
rosrun imu_utils imu_an \
    _imu_topic:="$IMU_TOPIC" \
    _imu_name:="$IMU_NAME" \
    _data_save_path:="$OUTDIR/" \
    _max_time_min:="$MAXMIN" \
    _max_cluster:=100 \
    >"$OUTDIR/imu_utils.log" 2>&1 &
IMU_AN_PID=$!
sleep 2

log "  rosbag play imu.bag (-r $PLAY_RATE)"
rosbag play -r "$PLAY_RATE" --clock "$IMU_BAG" >>"$OUTDIR/imu_play.log" 2>&1

log "  等待 imu_an 收尾计算..."
WAITED=0
while kill -0 "$IMU_AN_PID" 2>/dev/null; do
    sleep 1; WAITED=$((WAITED+1))
    [ "$WAITED" -gt 180 ] && { err "imu_an 超时强杀"; kill "$IMU_AN_PID" 2>/dev/null; break; }
done
kill "$ROSCORE_PID" 2>/dev/null; wait "$ROSCORE_PID" 2>/dev/null

# 兜底: 若因 imu_name 解析问题落成 _imu_param.yaml, 用 glob 找
if [ ! -f "$IMU_PARAM" ]; then
    CAND=$(ls "$OUTDIR"/*_imu_param.yaml 2>/dev/null | head -1)
    [ -n "$CAND" ] && IMU_PARAM="$CAND"
fi
[ -f "$IMU_PARAM" ] || { err "imu_utils 未产出 *_imu_param.yaml"; mkdir -p "$RESULT_DST"; cp "$OUTDIR"/*.log "$RESULT_DST/" 2>/dev/null; exit 20; }
python3 "$SCRIPTS/imu_param_to_kalibr.py" --in "$IMU_PARAM" --out "$IMU_YAML" \
    --rate "$IMU_RATE" --safety "$IMU_SAFETY" || exit 21
log "步骤1 完成 -> imu.yaml"

############################################
# 步骤 2: 两个 calibration bag 分别转换为 kalibr 输入
############################################
log "步骤2a: 转换 calibration_4cam.bag (4 cam, @$([ "$CAM_CONVERT_HZ" = 0 ] && echo 全帧 || echo ${CAM_CONVERT_HZ}Hz), 不复制IMU)"
# 进度实时显示到终端并存日志; PIPESTATUS[0] 取 python 的退出码(而非 tee 的)
CONVERT_START=$SECONDS
stdbuf -oL -eL python3 "$SCRIPTS/convert_to_kalibr.py" \
    --input "$CAL_4CAM_BAG" --output "$CAM_CONV_BAG" --cam-hz "$CAM_CONVERT_HZ" --no-imu \
    2>&1 | tee "$OUTDIR/convert_4cam.log"
[ "${PIPESTATUS[0]}" -eq 0 ] && [ -s "$CAM_CONV_BAG" ] || {
    err "四相机 bag 转换失败, 见 convert_4cam.log"; tail -5 "$OUTDIR/convert_4cam.log"; exit 30; }
log "步骤2a 完成，耗时 $((SECONDS-CONVERT_START))s ($(du -h "$CAM_CONV_BAG" | cut -f1))"

log "步骤2b: 转换 calibration_cam0_imu.bag (仅 cam0 + IMU, @$([ "$IMUCAM_CONVERT_HZ" = 0 ] && echo 全帧 || echo ${IMUCAM_CONVERT_HZ}Hz))"
CONVERT_START=$SECONDS
stdbuf -oL -eL python3 "$SCRIPTS/convert_to_kalibr.py" \
    --input "$CAL_IMUCAM_BAG" --output "$IMUCAM_CONV_BAG" --cam-hz "$IMUCAM_CONVERT_HZ" \
    --camera-indices 0 \
    2>&1 | tee "$OUTDIR/convert_cam0_imu.log"
[ "${PIPESTATUS[0]}" -eq 0 ] && [ -s "$IMUCAM_CONV_BAG" ] || {
    err "cam0-IMU bag 转换失败, 见 convert_cam0_imu.log"; tail -5 "$OUTDIR/convert_cam0_imu.log"; exit 32; }
log "步骤2b 完成，耗时 $((SECONDS-CONVERT_START))s ($(du -h "$IMUCAM_CONV_BAG" | cut -f1))"

############################################
# 步骤 3: 多目相机内/外参 (4 目联合, 产出相机间外参链)
############################################
CAMCHAIN="$OUTDIR/kalibr_input-camchain.yaml"
log "步骤3: kalibr_calibrate_cameras (models: $MODELS)"
CAM_CALIB_EXTRA_ARGS=(
    --freeze-intrinsics-rmse "$CAM_FREEZE_INTRINSICS_RMSE"
    --freeze-intrinsics-min-views "$CAM_FREEZE_INTRINSICS_MIN_VIEWS"
    --freeze-intrinsics-stable-views "$CAM_FREEZE_INTRINSICS_STABLE_VIEWS"
)
[ "$CAM_USE_BLAKE_ZISSERMAN" = "1" ] && CAM_CALIB_EXTRA_ARGS+=(--use-blakezisserman)
[ "$CAM_NO_SHUFFLE" = "1" ] && CAM_CALIB_EXTRA_ARGS+=(--no-shuffle)
log "  内参冻结: axis-RMSE<=${CAM_FREEZE_INTRINSICS_RMSE}px, 至少${CAM_FREEZE_INTRINSICS_MIN_VIEWS}个已接纳视图, 连续${CAM_FREEZE_INTRINSICS_STABLE_VIEWS}次达标"
# 进度条实时显示到终端, 同时存日志 (stdbuf 关闭管道缓冲, 保证 \r 进度逐帧刷新)
if ! ( cd "$OUTDIR" && \
  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_cameras \
    --bag "$CAM_CONV_BAG" \
    --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw \
    --models $MODELS \
    --target "$TARGET" \
    --bag-freq "$CAM_BAG_FREQ" \
    "${CAM_CALIB_EXTRA_ARGS[@]}" \
    --dont-show-report \
    2>&1 | tee "$OUTDIR/cam_calib.log" ); then
    err "相机标定命令失败, 见 cam_calib.log"
    mkdir -p "$RESULT_DST"; cp "$OUTDIR/cam_calib.log" "$IMU_YAML" "$RESULT_DST/" 2>/dev/null
    exit 31
fi
# kalibr 按 bag 路径派生输出名, 写到 bag 所在目录($CAM_WORK)而非 CWD; 移进 OUTDIR 以便检查与回传。
# kalibr_input.bag 不含连字符, 不会被 kalibr_input-* 误匹配。
mv "$CAM_WORK"/kalibr_input-* "$OUTDIR"/ 2>/dev/null
[ -f "$CAMCHAIN" ] || { err "相机标定失败, 见 cam_calib.log"; tail -15 "$OUTDIR/cam_calib.log"; \
    mkdir -p "$RESULT_DST"; cp "$OUTDIR"/*.log "$IMU_YAML" "$RESULT_DST/" 2>/dev/null; exit 31; }
log "步骤3 完成 -> camchain.yaml"

############################################
# 步骤 4: Camera-IMU 联合标定 (仅用 cam0)
############################################
# 只取步骤3 camchain 里的 cam0 喂给步骤4:
# 四目相机间外参(T_cn_cnm1)中, 大角度相对外参精度较差,
# 若带进 cam-imu 联合标定会污染结果。故仅标单相机-IMU 外参。
# 用 cam0(链头): cam0 本身无 T_cn_cnm1, 键名也天然从 cam0 起,
# 正符合 Kalibr 单相机 imu 标定"键从 cam0 起"的要求。
CAMCHAIN_IMUCAM_SRC="$OUTDIR/kalibr_input-camchain-cam0.yaml"
python3 -c "
import yaml, sys
d = yaml.safe_load(open('$CAMCHAIN'))
c0 = d['cam0']
c0.pop('T_cn_cnm1', None)   # cam0 本无此键; 保险起见 pop, 缺失则跳过
c0['cam_overlaps'] = []
yaml.safe_dump({'cam0': c0}, open('$CAMCHAIN_IMUCAM_SRC','w'), default_flow_style=False, sort_keys=False)
" || { err "提取 cam0 camchain 失败"; exit 40; }
log "  步骤4 仅用 cam0 (camchain-cam0.yaml)"

IMUCAM="$OUTDIR/kalibr_input-camchain-imucam.yaml"
log "步骤4: kalibr_calibrate_imu_camera"
if ! ( cd "$OUTDIR" && \
  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_imu_camera \
    --bag "$IMUCAM_CONV_BAG" \
    --cam "$CAMCHAIN_IMUCAM_SRC" \
    --imu "$IMU_YAML" \
    --target "$TARGET" \
    --bag-freq "$IMUCAM_BAG_FREQ" \
    --max-iter 100 \
    --timeoffset-padding 0.03 \
    --dont-show-report \
    2>&1 | tee "$OUTDIR/imucam_calib.log" ); then
    err "cam-imu 标定命令失败, 见 imucam_calib.log"
    mkdir -p "$RESULT_DST"; cp "$OUTDIR"/* "$RESULT_DST/" 2>/dev/null
    exit 41
fi
# 同上: kalibr 把 camchain-imucam.yaml 写到 bag 所在目录, 移进 OUTDIR。
mv "$IMUCAM_WORK"/kalibr_input-* "$OUTDIR"/ 2>/dev/null
[ -f "$IMUCAM" ] || { err "cam-imu 标定失败, 见 imucam_calib.log"; tail -15 "$OUTDIR/imucam_calib.log"; \
    mkdir -p "$RESULT_DST"; cp "$OUTDIR"/* "$RESULT_DST/" 2>/dev/null; exit 41; }
log "步骤4 完成 -> camchain-imucam.yaml"

############################################
# 步骤 5: 结果质量汇总 (解析重投影误差 / timeshift, 生成 summary.txt)
############################################
log "步骤5: 汇总标定质量"
REPROJ_WARN="${REPROJ_WARN:-1.0}"
python3 "$SCRIPTS/summarize_result.py" --dir "$OUTDIR" \
    --reproj-warn "$REPROJ_WARN" --out "$OUTDIR/summary.txt"
SUMRC=$?
case "$SUMRC" in
    0) log "质量汇总: 全部通过 ✅" ;;
    2) err "质量汇总: 存在 WARN (重投影误差偏大), 见 summary.txt" ;;
    *) err "质量汇总: 解析异常/缺文件, 见 summary.txt" ;;
esac

############################################
# 收尾: 结果整体 cp 回 goosefs
############################################
if [ "$KEEP_CONVERTED" = "1" ]; then
    mv "$CAM_CONV_BAG" "$OUTDIR/kalibr_input_4cam.bag" || { err "保留四相机转换 bag 失败"; exit 50; }
    mv "$IMUCAM_CONV_BAG" "$OUTDIR/kalibr_input_cam0_imu.bag" || { err "保留 cam0-IMU 转换 bag 失败"; exit 50; }
else
    rm -f "$CAM_CONV_BAG" "$IMUCAM_CONV_BAG"
fi

log "拷贝结果到 $RESULT_DST/"
rm -rf "$RESULT_DST" 2>/dev/null
mkdir -p "$RESULT_DST" || { err "创建结果目录失败: $RESULT_DST"; exit 51; }
# 逐文件 cp (goosefs 不支持 cp -a 的属性操作)
for f in "$OUTDIR"/*; do
    [ -f "$f" ] || continue
    cp "$f" "$RESULT_DST/$(basename "$f")" || { err "复制结果失败: $f"; exit 52; }
done
echo "done $(date)" > "$WORK/.done_tmp" && cp "$WORK/.done_tmp" "$RESULT_DST/.done" || {
    err "写入完成标记失败: $RESULT_DST/.done"; exit 53; }

rm -rf "$WORK"
log "全部完成 ✅  结果在 $RESULT_DST/"
