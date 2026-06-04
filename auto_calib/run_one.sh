#!/bin/bash
# run_one.sh  ——  在 Docker 容器内对单个数据文件夹做完整标定
# 用法 (容器内):  bash run_one.sh /data/111
#
# 重要: /data 是 goosefs 对象存储 FUSE, 只支持"整文件写入(cp)", 不支持 append/重写。
#       因此所有中间计算都在容器本地 /work 下完成, 最后把结果 cp 回 /data/<folder>/result/。
#
# 期望输入:  <folder>/calibration.bag   <folder>/imu.bag
# 产出 (cp 到 <folder>/result/):
#   imu.yaml
#   kalibr_input-camchain.yaml           (相机内外参)
#   kalibr_input-camchain-imucam.yaml    (cam-imu 外参, 最终结果)
#   <name>_imu_param.yaml, *.log, *.pdf  (过程文件)
set -o pipefail

DATA_FOLDER="${1:?用法: run_one.sh /data/<folder>}"
CAM_HZ="${CAM_HZ:-0}"            # 转换时相机抽帧频率, 0=不降采样(保留全帧, 利于多目共视)
BAG_FREQ="${BAG_FREQ:-10}"       # 传给 kalibr 的 --bag-freq, 即标定时处理频率
IMU_RATE="${IMU_RATE:-200}"
IMU_SAFETY="${IMU_SAFETY:-1.0}"
MODELS="${MODELS:-eucm-none eucm-none eucm-none eucm-none}"
# 标定板文件名 (checkerboard.yaml / aprilgrid.yaml)
TARGET_NAME="${TARGET_NAME:-checkerboard.yaml}"
# TARGET_SRC 可显式指定标定板路径; 留空则在多个候选位置自动查找
TARGET_SRC="${TARGET_SRC:-}"
PLAY_RATE="${PLAY_RATE:-50}"
SCRIPTS="${SCRIPTS:-/opt/auto_calib}"
KEEP_CONVERTED="${KEEP_CONVERTED:-0}"

NAME="$(basename "$DATA_FOLDER")"
CAL_BAG="$DATA_FOLDER/calibration.bag"
IMU_BAG="$DATA_FOLDER/imu.bag"
RESULT_DST="$DATA_FOLDER/result"      # 最终结果落地 (goosefs, 整文件 cp)

WORK="/work/$NAME"                    # 容器本地工作区
OUTDIR="$WORK/out"                    # 本地输出, 跑完整体 cp 回 RESULT_DST
CONV_BAG="$WORK/kalibr_input.bag"     # 转换后的大 bag (本地)

log(){ echo -e "\033[1;36m[$(date +%H:%M:%S)][$NAME] $*\033[0m"; }
err(){ echo -e "\033[1;31m[$(date +%H:%M:%S)][$NAME][ERR] $*\033[0m"; }

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"
export ROS_HOSTNAME="${ROS_HOSTNAME:-localhost}"
export PYTHONUNBUFFERED=1          # 让 kalibr 的进度条实时刷新, 不被行缓冲卡住
source /opt/ros/noetic/setup.bash
source /catkin_ws/devel/setup.bash

[ -f "$CAL_BAG" ] || { err "缺少 $CAL_BAG"; exit 10; }
[ -f "$IMU_BAG" ] || { err "缺少 $IMU_BAG"; exit 11; }

# 完成标记 (在 goosefs 上, 整文件 cp 写入)
if [ -f "$RESULT_DST/.done" ]; then
    log "已存在 result/.done, 跳过 (删除可重跑)"
    exit 0
fi

rm -rf "$WORK"; mkdir -p "$OUTDIR" /work/board
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
cp "$TARGET_SRC" /work/board/ || { err "拷贝标定板失败: $TARGET_SRC"; exit 12; }
TARGET="/work/board/$(basename "$TARGET_SRC")"
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
    _imu_topic:=/imu_data_raw \
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
# 步骤 2: calibration.bag -> kalibr 输入
############################################
log "步骤2: 转换 calibration.bag (cam->mono8@$([ "$CAM_HZ" = 0 ] && echo 全帧 || echo ${CAM_HZ}Hz), imu->/imu0)"
# 进度实时显示到终端并存日志; PIPESTATUS[0] 取 python 的退出码(而非 tee 的)
stdbuf -oL -eL python3 "$SCRIPTS/convert_to_kalibr.py" \
    --input "$CAL_BAG" --output "$CONV_BAG" --cam-hz "$CAM_HZ" \
    2>&1 | tee "$OUTDIR/convert.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { err "转换失败, 见 convert.log"; tail -5 "$OUTDIR/convert.log"; exit 30; }
log "步骤2 完成 ($(du -h "$CONV_BAG" | cut -f1))"

############################################
# 步骤 3: 多目相机内/外参 (4 目联合, 产出相机间外参链)
############################################
CAMCHAIN="$OUTDIR/kalibr_input-camchain.yaml"
log "步骤3: kalibr_calibrate_cameras (models: $MODELS)"
# 进度条实时显示到终端, 同时存日志 (stdbuf 关闭管道缓冲, 保证 \r 进度逐帧刷新)
( cd "$OUTDIR" && \
  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_cameras \
    --bag "$CONV_BAG" \
    --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw \
    --models $MODELS \
    --target "$TARGET" \
    --bag-freq "$BAG_FREQ" \
    --dont-show-report \
    2>&1 | tee "$OUTDIR/cam_calib.log" )
# kalibr 按 bag 路径派生输出名, 写到 bag 所在目录($WORK)而非 CWD; 移进 OUTDIR 以便检查与回传。
# kalibr_input.bag 不含连字符, 不会被 kalibr_input-* 误匹配。
mv "$WORK"/kalibr_input-* "$OUTDIR"/ 2>/dev/null
[ -f "$CAMCHAIN" ] || { err "相机标定失败, 见 cam_calib.log"; tail -15 "$OUTDIR/cam_calib.log"; \
    mkdir -p "$RESULT_DST"; cp "$OUTDIR"/*.log "$IMU_YAML" "$RESULT_DST/" 2>/dev/null; exit 31; }
log "步骤3 完成 -> camchain.yaml"

############################################
# 步骤 4: Camera-IMU 联合标定 (仅用 cam0)
############################################
# 只取步骤3 camchain 里的 cam0 喂给步骤4:
# cam2/cam3 为后向相机, 与 cam0/cam1 几乎无共视, 其相机间外参(T_cn_cnm1)不可靠,
# 若带进 cam-imu 联合标定会污染结果。故仅标 cam0-IMU 外参。
CAMCHAIN_CAM0="$OUTDIR/kalibr_input-camchain-cam0.yaml"
python3 -c "
import yaml, sys
d = yaml.safe_load(open('$CAMCHAIN'))
c0 = d['cam0']
c0.pop('T_cn_cnm1', None)   # cam0 本无此键, 保险
c0['cam_overlaps'] = []
yaml.safe_dump({'cam0': c0}, open('$CAMCHAIN_CAM0','w'), default_flow_style=False, sort_keys=False)
" || { err "提取 cam0 camchain 失败"; exit 40; }
log "  步骤4 仅用 cam0 (camchain-cam0.yaml)"

IMUCAM="$OUTDIR/kalibr_input-camchain-imucam.yaml"
log "步骤4: kalibr_calibrate_imu_camera"
( cd "$OUTDIR" && \
  stdbuf -oL -eL rosrun kalibr kalibr_calibrate_imu_camera \
    --bag "$CONV_BAG" \
    --cam "$CAMCHAIN_CAM0" \
    --imu "$IMU_YAML" \
    --target "$TARGET" \
    --bag-freq "$BAG_FREQ" \
    --max-iter 50 \
    --dont-show-report \
    2>&1 | tee "$OUTDIR/imucam_calib.log" )
# 同上: kalibr 把 camchain-imucam.yaml 写到 bag 所在目录, 移进 OUTDIR。
mv "$WORK"/kalibr_input-* "$OUTDIR"/ 2>/dev/null
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
log "拷贝结果到 $RESULT_DST/"
rm -rf "$RESULT_DST" 2>/dev/null
mkdir -p "$RESULT_DST"
# 逐文件 cp (goosefs 不支持 cp -a 的属性操作)
for f in "$OUTDIR"/*; do
    [ -f "$f" ] && cp "$f" "$RESULT_DST/$(basename "$f")"
done
echo "done $(date)" > "$WORK/.done_tmp" && cp "$WORK/.done_tmp" "$RESULT_DST/.done"

[ "$KEEP_CONVERTED" != "1" ] && rm -f "$CONV_BAG"
rm -rf "$WORK"
log "全部完成 ✅  结果在 $RESULT_DST/"
