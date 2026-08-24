# Kalibr 四目鱼眼相机 + IMU 自动标定平台

本项目基于 [ETH Zurich Kalibr](https://github.com/ethz-asl/kalibr) 二次开发，面向四目鱼眼相机与 IMU 的工程化标定。仓库在保留 Kalibr 多相机、Camera–IMU 时空标定能力的基础上，提供三类 ROS Bag 自动预处理、IMU Allan 方差分析、四相机内外参标定、cam0–IMU 联合标定、结果质量汇总，以及可视化 Web 任务中心。日常使用无需手工拼接多条 Kalibr 命令：准备好规定的数据目录后，即可通过脚本或网页完成整套流程。

> 当前生产环境：ROS Noetic、Docker 容器 `kalibr_work`、默认数据目录 `/home/conanluo/kalibr_data`、Web 端口 `8020`。

## 主要功能

- 四目相机内参和相邻相机外参联合标定
- cam0 与 IMU 的空间外参和时间偏移标定
- 基于 `imu_utils` Allan 方差的 IMU 噪声参数估计
- JPEG、PNG、H.264 压缩图像转 Kalibr `mono8` Bag
- 相机独立抽帧、H.264 多路并行解码和关键帧起始保护
- 多数据集串行批处理，单个数据集失败时继续后续任务
- `summary.txt` 自动汇总重投影误差、时间偏移和质量结论
- Web 端数据发现、任务排队、实时日志、暂停/继续、结果查看与下载
- 支持 `pinhole`、`omni`、EUCM、Double Sphere 等相机模型，以及 Checkerboard、AprilGrid、ChArUco 标定板

## 标定流程

```text
imu.bag ───────────────> imu_utils Allan 方差 ───────────────> imu.yaml

calibration_4cam.bag ──> 解码/抽帧/Topic 转换 ─> 四相机标定 ─> camchain.yaml

calibration_cam0_imu.bag
          + imu.yaml
          + cam0 内参 ──> Camera–IMU 联合标定 ──────────────> camchain-imucam.yaml

上述结果 ──────────────> 质量解析 ───────────────────────────> summary.txt
```

为避免对象存储或 FUSE 文件系统不支持追加写入，中间计算统一在容器本地 `/work/<数据集名>` 完成，结束后再逐文件复制到数据集的 `result/` 目录。

## 目录结构

```text
kalibr/
├── auto_calib/                 # 自动标定脚本、Bag 转换和结果汇总
│   ├── run_all.sh              # 宿主机入口：一个或多个数据集
│   ├── run_one.sh              # 容器内入口：单个数据集完整流程
│   ├── convert_to_kalibr.py    # 压缩图像/IMU 转 Kalibr Bag
│   ├── setup_imu_utils.sh      # 检查并准备 imu_utils、PyAV
│   └── summarize_result.py     # 生成质量摘要
├── calibration_board_data/     # 标定板配置
│   └── checkerboard.yaml
├── calibration_web/            # React + FastAPI 标定任务中心
│   ├── frontend/
│   ├── backend/
│   ├── start.sh
│   └── README.md
├── aslam_cv/                    # Kalibr 相机模型、标定板等核心模块
├── aslam_offline_calibration/   # Kalibr 标定命令与 Python 实现
├── Dockerfile_ros1_20_04        # ROS Noetic / Ubuntu 20.04 镜像
└── LICENSE
```

## 数据准备

默认情况下，每个待标定数据集必须包含三份互相独立的 Bag：

```text
/home/conanluo/kalibr_data/
└── <数据集名>/
    ├── calibration_4cam.bag       # 四路相机，共视标定板，用于相机内外参
    ├── calibration_cam0_imu.bag   # cam0 + IMU，充分运动激励，用于 Camera–IMU
    └── imu.bag                    # IMU 静置数据，用于 Allan 方差
```

脚本不会把单个 `calibration.bag` 自动拆成上述三份文件。文件名必须完全一致，否则该目录不会被自动发现。

### Topic 约定

转换脚本同时兼容两套相机 Topic，并统一输出为 Kalibr Topic：

| 物理位置 | 旧设备输入 Topic | 新设备输入 Topic | Kalibr 输出 Topic |
| --- | --- | --- | --- |
| 后左 | `/fisheye/bleft/image_raw/compressed` | `/cam0/image/compressed` | `/cam0/image_raw` |
| 左 | `/fisheye/left/image_raw/compressed` | `/cam1/image/compressed` | `/cam1/image_raw` |
| 右 | `/fisheye/right/image_raw/compressed` | `/cam2/image/compressed` | `/cam2/image_raw` |
| 后右 | `/fisheye/bright/image_raw/compressed` | `/cam3/image/compressed` | `/cam3/image_raw` |
| IMU（cam0–IMU Bag） | `/imu_data_raw` 或 `/imu/data_raw` | 同左 | `/imu0` |

四路相机必须按 `后左 → 左 → 右 → 后右` 排列。Kalibr 依赖相邻相机具有足够共视区域，Topic 与物理位置不一致会直接影响外参收敛。

其中 `imu.bag` 由 `imu_utils` 直接读取，当前批处理默认要求静置 IMU Topic 为 `/imu_data_raw`；上表的多个 IMU 名称兼容范围用于 `calibration_cam0_imu.bag` 的转换阶段。

录制数据时还应注意：

- `calibration_4cam.bag`：标定板需覆盖各相机画面中心、边缘和不同距离/姿态，相邻相机要有足够的共同观测。
- `calibration_cam0_imu.bag`：cam0 持续看到标定板，同时绕三个轴进行充分但不过快的旋转和平移。
- `imu.bag`：设备全程静止，时长应满足 Allan 方差分析要求；脚本默认以 12 分钟为最大分析窗口。
- 图像消息需为 `sensor_msgs/CompressedImage`；当前支持 JPEG、PNG 和 H.264。
- 使用前务必核对 [checkerboard.yaml](calibration_board_data/checkerboard.yaml) 中的内角点数量和实际尺寸。

## 快速开始

### 1. 启动标定容器

宿主机需要安装 Docker，并准备本仓库、`imu_utils` 目录和数据目录：

```bash
docker run -it --rm --name kalibr_work --entrypoint /bin/bash \
  -v /root/kalibr:/catkin_ws/src/kalibr \
  -v /root/imu_utils:/catkin_ws/src/imu_utils \
  -v /home/conanluo/kalibr_data:/data \
  luohongkun0715/kalibr_and_imu_utils:noetic-fixed
```


自动流程还依赖 `imu_utils` 和 PyAV。`run_all.sh` 首次运行时会调用 `setup_imu_utils.sh` 检查并安装缺失项，因此首次执行可能需要较长时间并要求容器能够访问软件源。

### 2. 运行命令行标定

标定指定数据集：

```bash
cd /root/kalibr
bash auto_calib/run_all.sh 0819_ECA_calib1_bag
```

一次标定多个数据集：

```bash
bash auto_calib/run_all.sh dataset_001 dataset_002 dataset_003
```

不传数据集名时，处理数据根目录下所有同时包含三份必需 Bag 的目录：

```bash
bash auto_calib/run_all.sh
```

使用其他宿主机数据目录或容器名时，宿主机目录必须与容器 `/data` 挂载源保持一致：

```bash
DATA_ROOT=/path/to/kalibr_data \
CONTAINER=kalibr_work \
bash auto_calib/run_all.sh dataset_001
```

### 3. 启动 Web 平台

先确认 `kalibr_work` 正在运行，然后执行：

```bash
cd /root/kalibr
bash calibration_web/start.sh
```

浏览器访问：

- 本机：<http://127.0.0.1:8020>
- 局域网：`http://<服务器IP>:8020`

Web 平台默认扫描 `/home/conanluo/kalibr_data`，前端、API 和实时日志共用 `8020` 端口。任务并发固定为 1，避免多个标定任务同时争用容器、CPU、内存和 `/work`。

新环境首次启动 Web 平台时，需要 Node.js 20+ 和 Python 3.10+，并初始化依赖：

```bash
python3 -m venv calibration_web/backend/.venv
calibration_web/backend/.venv/bin/pip install -e calibration_web/backend
npm --prefix calibration_web/frontend ci
```

常用 Web 配置：

```bash
EGO_WEB_DATA_ROOT=/path/to/kalibr_data \
EGO_WEB_HOST=0.0.0.0 \
EGO_WEB_PORT=8020 \
bash calibration_web/start.sh
```

更多页面和任务控制说明见 [calibration_web/README.md](calibration_web/README.md)。

## 输出结果

标定完成后，结果位于 `<数据集名>/result/`：

| 文件 | 说明 |
| --- | --- |
| `imu.yaml` | Kalibr 格式的 IMU 噪声密度、随机游走和采样率 |
| `kalibr_input-camchain.yaml` | 四路相机内参及相邻相机外参链 |
| `kalibr_input-camchain-imucam.yaml` | cam0 与 IMU 的空间外参及时间偏移 |
| `summary.txt` | 重投影误差、timeshift 和 OK/WARN/FAIL 结论 |
| `*-report-*.pdf` | Kalibr 生成的相机或 Camera–IMU 标定报告 |
| `*.log` | Bag 转换、IMU、相机和 Camera–IMU 各阶段日志 |
| `.done` | 完成标记；存在时脚本会跳过该数据集 |

`kalibr_input-camchain-imucam.yaml` 只包含 cam0–IMU 结果；完整四相机外参仍需配合 `kalibr_input-camchain.yaml` 使用。

`summary.txt` 的总体结论含义：

- `OK`：四路相机重投影误差均未超过阈值。
- `WARN`：存在重投影误差偏大或观测不足，流程已完成但需要人工复核 PDF 和日志。
- `FAIL`：缺少关键输出或结果无法解析。

## 常用参数

参数以环境变量形式放在 `bash auto_calib/run_all.sh ...` 前。以下默认值以当前脚本为准：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `DATA_ROOT` | `/home/conanluo/kalibr_data` | 宿主机数据根目录 |
| `CONTAINER` | `kalibr_work` | 标定容器名 |
| `DATA_MNT` | `/data` | 数据根在容器内的挂载位置 |
| `CAM_BAG_FREQ` | `1` | 四相机 Kalibr 标定频率 |
| `IMUCAM_BAG_FREQ` | `30` | cam0–IMU Kalibr 标定频率 |
| `CAM_CONVERT_HZ` | 同 `CAM_BAG_FREQ` | 四相机 Bag 转换保留频率；`0` 表示全帧 |
| `IMUCAM_CONVERT_HZ` | 同 `IMUCAM_BAG_FREQ` | cam0–IMU Bag 图像保留频率；`0` 表示全帧 |
| `IMU_RATE` | `200` | 写入 `imu.yaml` 的 IMU 更新频率 |
| `IMU_SAFETY` | `1.0` | IMU 噪声和随机游走安全放大系数 |
| `MODELS` | 四路 `eucm-none` | cam0～cam3 的相机模型 |
| `TARGET_NAME` | `checkerboard.yaml` | `calibration_board_data/` 中的标定板文件名 |
| `CAM_FREEZE_INTRINSICS_RMSE` | `0.2` | 内参冻结的单轴重投影 RMSE 阈值，单位 px |
| `CAM_FREEZE_INTRINSICS_MIN_VIEWS` | `30` | 允许冻结内参前的最少有效多相机视图数 |
| `CAM_FREEZE_INTRINSICS_STABLE_VIEWS` | `5` | 达到阈值后要求连续稳定的视图数 |
| `CAM_USE_BLAKE_ZISSERMAN` | `1` | 是否使用 Blake–Zisserman 稳健核 |
| `CAM_NO_SHUFFLE` | `0` | `1` 表示按时间顺序处理视图 |
| `KALIBR_EXTRACT_JOBS` | `16` | 标定板角点提取进程数 |
| `KALIBR_OPT_THREADS` | `16` | 相机标定优化器线程数 |
| `PLAY_RATE` | `50` | `imu.bag` 回放倍速 |
| `REPROJ_WARN` | `1.0` | 相机平均重投影误差告警阈值，单位 px |
| `KEEP_CONVERTED` | `0` | `1` 表示保留转换后的大体积 Kalibr Bag |

例如，使用 Double Sphere 模型、降低四相机处理频率并放大 IMU 噪声参数：

```bash
CAM_BAG_FREQ=0.5 \
IMUCAM_BAG_FREQ=30 \
IMU_SAFETY=5 \
MODELS="ds-none ds-none ds-none ds-none" \
bash auto_calib/run_all.sh dataset_001
```

`CAM_HZ` 和 `BAG_FREQ` 仅用于兼容旧调用。新任务建议分别设置 `CAM_CONVERT_HZ`、`IMUCAM_CONVERT_HZ`、`CAM_BAG_FREQ` 和 `IMUCAM_BAG_FREQ`。

## 重跑与排障

### 重跑数据集

脚本检测到 `result/.done` 后会直接跳过。需要重跑时，可在 Web 页面点击“重置标定”，或手动移走/删除该数据集的 `result/` 目录。操作前确认其中没有需要保留的结果。

### 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 找不到可标定目录 | 三个 Bag 的文件名、`DATA_ROOT`、宿主机目录与容器 `/data` 挂载是否一致 |
| `容器 kalibr_work 未运行` | 执行 `docker start kalibr_work`，再用 `docker ps` 检查 |
| 找不到标定板 | `calibration_board_data/checkerboard.yaml` 是否存在且尺寸配置正确 |
| `imu_utils` 无输出 | `imu.bag` 是否静置、时长是否足够、IMU Topic 是否为 `/imu_data_raw`；查看 `imu_utils.log` |
| H.264 解码失败或开头丢帧 | Bag 是否包含 SPS/PPS 和 IDR 关键帧；转换器会跳过首个关键帧前的 P 帧 |
| 相机标定 `not enough observations` | 每路相机是否都看到标定板，相邻相机是否有共同观测，模型和标定板配置是否正确 |
| Camera–IMU 不收敛 | cam0 是否持续看到标定板，三轴运动激励是否充分，时间戳和 IMU 单位是否正确 |
| `summary.txt` 为 WARN | 查看 `*-report-*.pdf`、`cam_calib.log` 和 `imucam_calib.log`，重点检查各相机重投影误差和观测覆盖 |
| Web 页面无法启动 | 后端虚拟环境、前端依赖、8020 端口占用和 `EGO_WEB_DATA_ROOT` 权限 |

建议按阶段日志定位问题：`convert_4cam.log` → `convert_cam0_imu.log` → `imu_utils.log` → `cam_calib.log` → `imucam_calib.log` → `summary.txt`。

## 相对上游 Kalibr 的重要改动

点补充了面向四目鱼眼与 IMU 的生产化能力：完善 EUCM/Double Sphere 和 OpenCV 4.x 兼容性，增加 ChArUco 标定板及 PDF 生成，修复共面点 PnP 初始化边界；相机标定加入视图乱序控制、Blake–Zisserman 稳健核、内参稳定后冻结、角点提取进程数与优化线程数限制；工程层新增三 Bag 独立标定、JPEG/PNG/H.264 转换、Allan 方差、质量汇总、批处理和 Web 任务中心。上述改动主要用于提高超广角、多相机、大数据量场景下的收敛稳定性、运行效率与可运维性。

