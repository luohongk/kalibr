# auto_calib —— 四目鱼眼 + IMU 批量自动标定

在 **Docker 容器内**对一批数据文件夹做端到端标定，每个文件夹产出：

- `imu.yaml` —— IMU 噪声参数（imu_utils Allan 方差自动标定）
- `kalibr_input-camchain.yaml` —— 4 路相机内参 + 相机间外参
- `kalibr_input-camchain-imucam.yaml` —— 相机与 IMU 时空外参（**最终结果**）
- `summary.txt` —— 重投影误差 / timeshift 汇总与好坏判定

与 `input_rosbag_data/`（手动单次流程，需人工预览裁剪）不同，本目录面向**无人值守批量**：
给定一组已录好的文件夹，一条命令跑完全部。

---

## 目录约定

```
<DATA_ROOT>/                       # 宿主机数据根, 挂载到容器 /data
├── 111/
│   ├── calibration.bag            # ★ 输入: 4 路相机 + IMU (相机激励运动, 看标定板)
│   ├── imu.bag                    # ★ 输入: 仅 IMU, 静置 (用于 Allan 方差)
│   └── result/                    #   产出 (脚本自动生成)
│       ├── imu.yaml
│       ├── kalibr_input-camchain.yaml
│       ├── kalibr_input-camchain-imucam.yaml
│       ├── summary.txt
│       ├── *.log  *.pdf  *_imu_param.yaml
│       └── .done                  #   完成标记, 重跑前需删除
├── 222/  ...
```

每个文件夹**必须**含 `calibration.bag` 和 `imu.bag`。`run_all.sh` 自动跳过不含 `calibration.bag` 的目录。

### 原始 topic 约定（与 `input_rosbag_data` 一致）

| 位置 | 原始 topic                               | Kalibr topic       |
| ---- | ---------------------------------------- | ------------------ |
| 左   | `/fisheye/left/image_raw/compressed`   | `/cam0/image_raw` |
| 右   | `/fisheye/right/image_raw/compressed`  | `/cam1/image_raw` |
| 后左 | `/fisheye/bleft/image_raw/compressed`  | `/cam2/image_raw` |
| 后右 | `/fisheye/bright/image_raw/compressed` | `/cam3/image_raw` |
| IMU  | `/imu_data_raw`                        | `/imu0`           |

---

## 快速开始

### 1) 启动容器（见仓库根 `readme_server.md`）

约定：仓库根挂到 `/catkin_ws/src/kalibr`，数据根挂到 `/data`。

```bash
docker run -it --rm --name kalibr_work --entrypoint /bin/bash \
  -v /root/kalibr:/catkin_ws/src/kalibr \
  -v /root/imu_utils:/catkin_ws/src/imu_utils \
  -v /home/conanluo/kalibr_data:/data \
  luohongkun0715/kalibr_and_imu_utils:noetic-fixed
```

宿主机：

docker cp /root/kalibr/. kalibr_work:/catkin_ws/src/kalibr/
docker exec -it kalibr_work /bin/bash
catkin build -DCMAKE_BUILD_TYPE=Release -j8


### 2) 宿主机一条命令批量跑

```bash
cd /root/kalibr/auto_calib

# 处理 DATA_ROOT 下所有含 calibration.bag 的文件夹
DATA_ROOT=/home/tione/notebook/dataset/lhk/kalibr_data IMU_SAFETY=1  bash run_all.sh 0604
DATA_ROOT=/home/conanluo/kalibr_data IMU_SAFETY=1  bash run_all.sh G1_2

DATA_ROOT=/home/tione/notebook/dataset/lhk/kalibr_data IMU_SAFETY=1  bash run_all.sh

# 或只跑指定文件夹
bash run_all.sh 111 222
```

`run_all.sh` 会：① 把本目录脚本 + `calibration_board_data/` 标定板用 `docker cp` 同步进容器 `/opt/auto_calib`；② 确保 `imu_utils` 已编译（`setup_imu_utils.sh`）；③ 逐文件夹调用 `run_one.sh`，单个失败不中断后续；④ 末尾打印成功/失败清单。

> **为什么用 `docker cp` 而不是 bind mount？** 本环境 bind mount 对脚本目录不可靠，且标定板放在仓库里、与数据挂载点 `/data` 无关，拷进容器最省心。

---

## 五步流程（`run_one.sh`，容器内单文件夹）

| 步骤 | 动作 | 关键产物 |
| ---- | ---- | -------- |
| ① | `imu_utils imu_an` 跑 `imu.bag` 做 Allan 方差 → `imu_param_to_kalibr.py` 转 Kalibr 格式 | `imu.yaml` |
| ② | `convert_to_kalibr.py`：4 路 JPEG→mono8、按 `CAM_HZ` 抽帧、IMU 透传，topic 改名 | `kalibr_input.bag`（本地 /work） |
| ③ | `kalibr_calibrate_cameras`（默认 `eucm-none`×4） | `*-camchain.yaml` |
| ④ | `kalibr_calibrate_imu_camera` | `*-camchain-imucam.yaml` |
| ⑤ | `summarize_result.py` 解析重投影误差/timeshift | `summary.txt` |

> **重要：写盘约定。** `/data` 是对象存储 FUSE，只支持整文件 `cp`，不支持 append/重写。
> 所以所有中间计算都在容器本地 `/work/<name>` 下完成，跑完再把 `out/` 逐文件 `cp` 回 `/data/<folder>/result/`。

### 标定板查找顺序

`run_one.sh` 按 `TARGET_NAME`（默认 `checkerboard.yaml`）在以下位置依次查找，命中即用：

1. `/opt/auto_calib/<TARGET_NAME>`（`run_all.sh` 从仓库 `calibration_board_data/` 拷入）
2. `/catkin_ws/src/kalibr/calibration_board_data/<TARGET_NAME>`（仓库挂载点）
3. `/data/calibration_board_data/<TARGET_NAME>`

也可用 `TARGET_SRC=/abs/path/board.yaml` 直接指定，跳过查找。

---

## 可调环境变量

通过 `run_all.sh` 透传，或直接 `export` 后调 `run_one.sh`：

| 变量 | 默认 | 说明 |
| ---- | ---- | ---- |
| `DATA_ROOT` | `/home/tione/notebook/dataset/lhk/kalibr_data` | 宿主机数据根 |
| `CONTAINER` | `kalibr_work` | 容器名 |
| `CAM_HZ` | `10` | 相机抽帧 / `--bag-freq` 频率（Hz） |
| `IMU_RATE` | `200` | IMU 频率，写进 `imu.yaml` 的 `update_rate` |
| `IMU_SAFETY` | `1.0` | imu_utils 噪声/随机游走安全放大系数（工程上常用 5~10） |
| `MODELS` | `eucm-none ×4` | 4 路相机模型，顺序对应 cam0~3 |
| `TARGET_NAME` | `checkerboard.yaml` | 标定板文件名（可换 `aprilgrid.yaml`） |
| `TARGET_SRC` | 空 | 显式标定板绝对路径，非空则跳过查找 |
| `PLAY_RATE` | `100` | imu.bag `rosbag play -r` 倍速（加快 Allan 方差） |
| `REPROJ_WARN` | `1.0` | 相机重投影误差(mean,px)告警阈值，`summarize_result.py` 用 |
| `KEEP_CONVERTED` | `0` | `1` 则保留转换后的大 bag |

示例：

```bash
CAM_HZ=4 IMU_SAFETY=5 MODELS="ds-none ds-none ds-none ds-none" bash run_all.sh 111
```

**相机模型选择**（FoV 越大越靠下）：`pinhole-radtan`（<100°）/ `omni-radtan`（~120°）/ `eucm-none`（160°+ 首选）/ `ds-none`（极大 FoV）。本项目鱼眼推荐 `eucm-none`。

---

## summary.txt 与退出码

`summarize_result.py` 解析 `*-results-cam.txt`、`*-results-imucam.txt`，输出每路重投影误差、cam-imu 残差、各路 `timeshift cam->imu`，并给总判定：

- **OK**（退出 0）：全部相机重投影 mean ≤ `REPROJ_WARN`
- **WARN**（退出 2）：有相机重投影偏大或观测不足 —— 不中断流程，但需人工复核
- **FAIL**（退出 3）：缺关键结果文件

`run_one.sh` 只记录判定到日志，不因 WARN 失败，便于批量跑完再统一看 `summary.txt`。

---

## 重跑 / 排错

- **重跑某文件夹**：删掉 `<folder>/result/.done` 再跑（或直接删整个 `result/`）。
- **失败后看日志**：失败时脚本会把 `*.log` 拷回 `result/`。常用：`cam_calib.log`、`imucam_calib.log`、`imu_utils.log`、`convert.log`。

| 报错 | 排查方向 |
| ---- | -------- |
| `缺少 .../calibration.bag` / `imu.bag` | 文件夹结构不对，确认两个 bag 都在 |
| `找不到标定板 checkerboard.yaml` | 仓库 `calibration_board_data/` 是否存在；或显式给 `TARGET_SRC` |
| `imu_utils 未产出 *_imu_param.yaml` | `imu.bag` 太短/topic 不是 `/imu_data_raw`；看 `imu_utils.log` |
| `相机标定失败` | 录制时某路没看到标定板（`not enough observations`）；或模型选错 |
| `cam-imu 标定失败 / Optimization diverged` | IMU 激励不足，或 `--timeoffset-padding` 太小（脚本里 0.05，可改大到 0.1） |
| summary 标 WARN | 重投影误差大，数据质量不佳，建议重录或换裁剪片段 |

---

## 文件清单

| 文件 | 作用 |
| ---- | ---- |
| `run_all.sh` | 宿主机批量驱动：同步脚本/标定板进容器，遍历文件夹 |
| `run_one.sh` | 容器内单文件夹五步流程 |
| `setup_imu_utils.sh` | 幂等确保 `imu_utils` 已编译（装 Ceres + catkin build） |
| `convert_to_kalibr.py` | `calibration.bag` → Kalibr 输入 bag（JPEG→mono8、抽帧、改 topic） |
| `imu_param_to_kalibr.py` | imu_utils 输出 → Kalibr `imu.yaml` |
| `summarize_result.py` | 解析结果 txt → `summary.txt` + 好坏判定 |
