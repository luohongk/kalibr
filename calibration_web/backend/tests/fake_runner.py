#!/usr/bin/env bash
set -u

dataset="${1:?dataset is required}"
dataset_root="$DATA_ROOT/$dataset"
result="$dataset_root/result"

{
    printf '%s\n' "$DATA_ROOT"
    printf '%s\n' "$dataset"
    printf '%s\n' "${RUNNER_PARENT_ENV:-}"
} > "$dataset_root/invocation.txt"

if [ "$dataset" = "skip" ]; then
    echo "已存在 result/.done, 跳过 (删除可重跑)"
    exit 0
fi

echo "步骤1: imu_utils Allan 方差标定"

if [ "$dataset" = "slow" ]; then
    sleep 30 &
    child=$!
    printf '%s\n' "$child" > "$dataset_root/child.pid"
    trap 'wait "$child" 2>/dev/null; exit 143' TERM
    wait "$child"
    exit 0
fi

if [ "$dataset" = "parent-exit-stdout" ]; then
    sleep 30 &
    child=$!
    printf '%s\n' "$child" > "$dataset_root/child.pid"
    printf '%s\n' "exiting" > "$dataset_root/parent-exited"
    exit 0
fi

if [ "$dataset" = "ignore-term" ]; then
    trap '' TERM
    sleep 30 &
    child=$!
    printf '%s\n' "$child" > "$dataset_root/child.pid"
    wait "$child"
    exit 0
fi

if [ "$dataset" = "live-flush" ]; then
    echo "live output marker"
    printf '%s\n' "blocked" > "$dataset_root/blocked"
    sleep 30
    exit 0
fi

if [ "$dataset" = "fail" ]; then
    echo ""
    for number in $(seq 1 25); do
        echo "fake error $number"
    done
    exit 7
fi

echo "步骤2a: 转换 calibration_4cam.bag"
echo "步骤2b: 转换 calibration_cam0_imu.bag"
echo "未知日志不应改变阶段"
echo "步骤3: kalibr_calibrate_cameras"
echo "步骤4: kalibr_calibrate_imu_camera"
echo "步骤5: 汇总标定质量"
echo "stderr is merged" >&2

mkdir -p "$result"
printf '%s\n' "imu" > "$result/imu.yaml"
printf '%s\n' "camera" > "$result/kalibr_input-camchain.yaml"
printf '%s\n' "imu-camera" > "$result/kalibr_input-camchain-imucam.yaml"

case "$dataset" in
    no-done)
        ;;
    missing-yaml)
        rm "$result/kalibr_input-camchain-imucam.yaml"
        printf '%s\n' "done" > "$result/.done"
        ;;
    empty-yaml)
        : > "$result/kalibr_input-camchain-imucam.yaml"
        printf '%s\n' "done" > "$result/.done"
        ;;
    symlink-yaml)
        printf '%s\n' "outside" > "$dataset_root/outside.yaml"
        rm "$result/kalibr_input-camchain-imucam.yaml"
        ln -s "$dataset_root/outside.yaml" "$result/kalibr_input-camchain-imucam.yaml"
        printf '%s\n' "done" > "$result/.done"
        ;;
    done-symlink)
        printf '%s\n' "outside" > "$dataset_root/outside.done"
        ln -s "$dataset_root/outside.done" "$result/.done"
        ;;
    *)
        printf '%s\n' "done" > "$result/.done"
        ;;
esac

echo "拷贝结果到 $result/"
exit 0
