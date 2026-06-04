# 四目鱼眼相机 + IMU 标定流程

本项目用于对**头戴式四目鱼眼相机 + IMU** 系统进行标定，最终产出：

- `camchain.yaml` —— 4 个相机的内参 + 相机间外参
- `camchain-imucam.yaml` —— 相机与 IMU 的时空外参（T_cam_imu、time offset）

底层标定工具是 [Kalibr](https://github.com/ethz-asl/kalibr)（在 Docker 中运行）；本仓库脚本主要负责**录包前后的预处理**：预览、裁剪、重命名 topic、解码 JPEG → mono8 等。

---

## 目录结构

```
fisheye4_imu_calib/
├── README.md                          # 本文档
├── requirements.txt                   # Python 依赖
├── imu.yaml                           # IMU 噪声参数（标定时输入给 Kalibr）
├── calibration_board_data/            # 标定板配置（checkerboard.yaml 等）
├── input_rosbag_data/                 # ★ 数据预处理脚本 + bag 文件
│   ├── origin_move_camera_imu.bag     #   原始录制（请自行放入）
│   ├── input_move_camera_imu.bag     #   裁剪后的 bag（layoffs_rosbag.py 输出）
│   ├── kalibr_input_imu.bag          #   Kalibr 输入 bag（to_kalibr.py 输出）
│   ├── view_rosbag.py                #   预览 4 路相机 + 打印 IMU
│   ├── check_freq_sync.py            #   检查各 topic 频率 / jitter
│   ├── layoffs_rosbag.py             #   按时间戳裁剪 bag
│   ├── to_kalibr.py                  #   重命名 topic、JPEG → mono8
│   └── print_imu.py                  #   导出 IMU 到 CSV
├── imu_calibration/                   # IMU 自标定（可选，与本主流程独立）
└── image/                             # README 引用的截图
```

---

## 总流程一览

```
                 ┌─────────────────────────────┐
 ① 采集          │  origin_move_camera_imu.bag │  (相机 + IMU，标定板固定，相机激励运动)
                 └────────────────┬────────────┘
                                  │
 ② 预览 / 检查    view_rosbag.py 、 check_freq_sync.py
                                  │
 ③ 裁剪          layoffs_rosbag.py
                                  ▼
                   input_move_camera_imu.bag       (保留高质量片段)
                                  │
 ④ 转 Kalibr 格式  to_kalibr.py        (CompressedImage → mono8, topic→/cam0..3)
                                  ▼
                     kalibr_input_imu.bag
                                  │
 ⑤ Docker 内标定    kalibr_calibrate_cameras       →  camchain.yaml
                   kalibr_calibrate_imu_camera     →  camchain-imucam.yaml
```

---

## 一、ROS Topic

原始 bag 中的 4 路鱼眼相机 + IMU topic：

| 位置 | 原始 topic                               | Kalibr 输入 topic   |
| ---- | ---------------------------------------- | ------------------- |
| 左   | `/fisheye/left/image_raw/compressed`   | `/cam0/image_raw` |
| 右   | `/fisheye/right/image_raw/compressed`  | `/cam1/image_raw` |
| 后左 | `/fisheye/bleft/image_raw/compressed`  | `/cam2/image_raw` |
| 后右 | `/fisheye/bright/image_raw/compressed` | `/cam3/image_raw` |
| IMU  | `/imu_data_raw` (`sensor_msgs/Imu`)  | `/imu0`           |

预期频率：相机 30 Hz、IMU 200 Hz。

---

## 二、环境配置

```bash
conda create -n calibration python=3.13 -y
conda activate calibration

# Python 3.13 上 PyPI 的 opencv-python 与 numpy 不兼容，
# 必须从 conda-forge 装 OpenCV：
conda install -c conda-forge "opencv>=4.11,<4.12" -y

# 其余依赖
pip install -r requirements.txt
# 注意：requirements.txt 中的 tqdm 一项请确认拼写为 tqdm（不是 tdqm）。
# 如果 pip 报 “No matching distribution found”，手动执行：
#     pip install tqdm
```

> **为什么不直接 pip 装 opencv？**
> PyPI 的 `opencv-python` 4.10 / 4.13 在 Python 3.13 上是坏的（abi3 wheel 与 cp313
> 不兼容，调用 `cv2.imdecode` / `cv2.putText` 会报 *“img is not a numpy array”*）。
> conda-forge 的 wheel 是为 cp313 显式编译的，不会有这个问题。
> Python ≤ 3.12 上则可以直接 `pip install "opencv-python>=4.10,<4.12"`。

---

## 三、采集要求

录制一段 **约 2~3 分钟** 的 bag，命名为 `origin_move_camera_imu.bag`，要求：

| 阶段                      | 动作                                                                                                                                                     |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **相机内参**        | 标定板**固定不动**；头戴相机做**横向 + 纵向 + 远近**的平移摆动，让标定板在每一路相机的画面里**充满各种位置和角度**（含畸变较大的边缘） |
| **Camera-IMU 外参** | 在能保证 4 路相机仍能看到标定板的前提下，做**三轴平移 + 三轴旋转** 的快速激励（避免缓慢匀速，否则 IMU 信号信噪比太差）                             |

**采一次就够**，上述两阶段可以连续录在同一个 bag 内。

---

## 四、数据预处理

下面所有命令都从 `input_rosbag_data/` 内执行：

```bash
cd input_rosbag_data
```

### 4.1 预览 bag，挑出可用片段

```bash
# 2x2 网格播放 4 路相机
python view_rosbag.py --bag origin_move_camera_imu.bag --play

# 仅打印 bag 元信息（topic、消息数、时长）
python view_rosbag.py --bag origin_move_camera_imu.bag --info

# 检查各 topic 的频率 / jitter（看相机是否真稳定 30 Hz、IMU 是否 200 Hz）
python check_freq_sync.py
```

播放快捷键：`空格` 暂停 · `←/→` 步进 · `+/-` 加减速 · `1/2/4` 切换倍速 · `q` 退出。

### 4.2 裁剪 bag

记下"开始 / 结束"两个 Unix 时间戳（秒），然后：

```bash
# 按绝对时间戳裁剪（推荐）
python layoffs_rosbag.py \
    -i origin_move_camera_imu.bag \
    -o input_move_camera_imu.bag \
    --start 1777259940 --end 1777260000 --absolute

# 或：不裁剪、原样改名（仅在数据已经很干净时使用）
python layoffs_rosbag.py \
    -i origin_move_camera_imu.bag \
    -o input_move_camera_imu.bag \
    --start 0
```

### 4.3 转换为 Kalibr 输入格式

把 `CompressedImage (JPEG)` 解码为 `sensor_msgs/Image (mono8)`，并把 topic 改成
`/cam0..3/image_raw`、`/imu0`：

```bash
# 默认线程数 = cpu_count / 2；--force 表示覆盖已有输出
python to_kalibr.py --workers 8 --force
```

输出：`input_rosbag_data/kalibr_input_imu.bag`（**注意：体积会膨胀 5~10 倍**，因为 JPEG → mono8 后不再压缩）。

> 如果磁盘紧张，可以减少 `--bag-freq`（在 Kalibr 命令里降采样，见下文），不必降采样这一步。

---

## 五、启动 Kalibr Docker 容器

宿主机执行（已带 GUI 转发）：

```bash
xhost +local:root && \
docker run -it \
  --network=host \
  --privileged \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -e DISPLAY=$DISPLAY \
  -v /home/lhk/workspace/kalibr:/catkin_ws/src/kalibr \
  -v /home/lhk/workspace/fisheye4_imu_calib/input_rosbag_data:/data \
  --name kalibr_work \
  -w / \
  kalibr:noetic
```

> **挂载约定**：宿主机 /home/lhk/workspace/fisheye4_imu_calib/input_rosbag_data → 容器内 `/data`。后续路径都从 `/data/...` 起算。
> 把 `kalibr_input_imu.bag`、`checkerboard.yaml`、`imu.yaml` 都放到 `/home/lhk/data/Calibration/...` 下即可。

进入容器后先编译一次 Kalibr：

```bash
source /opt/ros/noetic/setup.bash
cd /catkin_ws
catkin build -DCMAKE_BUILD_TYPE=Release
source devel/setup.bash
```

---

## 六、Kalibr 标定

### 6.1 多目相机内 / 外参

```bash
rosrun kalibr kalibr_calibrate_cameras \
  --bag    /data/Calibration/CCC0527采集的数据/kalibr_input_imu.bag \
  --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw \
  --models eucm-none eucm-none eucm-none eucm-none \
  --target /data/Calibration/scripts/checkerboard.yaml \
  --bag-freq 10 \
  --dont-show-report
```

**相机模型如何选？**

| `--models`       | 全名                | 适用场景                                          |
| ------------------ | ------------------- | ------------------------------------------------- |
| `pinhole-radtan` | 针孔 + 径切畸变     | 普通透视相机（FoV < 100°）                       |
| `omni-radtan`    | Mei 全向 + 径切畸变 | 中等 FoV 鱼眼（~120°）；本项目早期版本使用       |
| `eucm-none`      | Enhanced Unified    | **大 FoV 鱼眼（160°+）首选**，参数少、稳定 |
| `ds-none`        | Double Sphere       | 极大 FoV / 折反射镜头，效果与 EUCM 接近           |

> 本项目用的镜头 FoV 较大，**推荐 `eucm-none`**。`omni-radtan` 只在历史脚本里保留。

**主要参数**

| 参数                   | 说明                                             |
| ---------------------- | ------------------------------------------------ |
| `--bag`              | 输入 bag                                         |
| `--topics`           | 4 路相机 topic                                   |
| `--models`           | 4 个相机模型，**顺序与 `--topics` 对应** |
| `--target`           | 标定板配置（见 `calibration_board_data/`）     |
| `--bag-freq`         | 从 bag 中重采样的频率，10 Hz 通常够且更快        |
| `--dont-show-report` | 不弹 PDF 报告（无 GUI 环境必加）                 |

完成后产出：`kalibr_input_imu-camchain.yaml`（即 `camchain.yaml`）。

### 6.2 Camera-IMU 联合标定

输入：

1. 6.1 的 `camchain.yaml`（4 路相机一起；如果 Kalibr 只接受单路，把 `cam0` 那段拷出来另存为 `cam0.yaml`）

   ![1780296339540](image/README/1780296339540.png)
2. `imu.yaml`（IMU 噪声参数；放在仓库根 `imu.yaml`）
3. 同一份 `kalibr_input_imu.bag`

```bash
rosrun kalibr kalibr_calibrate_imu_camera \
  --bag    /data/Calibration/kalibr_input_imu.bag \
  --cam    /data/Calibration/kalibr_input_imu-camchain.yaml \
  --imu    /data/Calibration/imu.yaml \
  --target /data/Calibration/checkerboard.yaml \
  --bag-freq 10.0 \
  --max-iter 50 \
  --timeoffset-padding 0.05
```

**主要参数**

| 参数                     | 说明                                                |
| ------------------------ | --------------------------------------------------- |
| `--cam`                | 相机标定结果（6.1 输出）                            |
| `--imu`                | IMU 噪声参数（continuous-time noise / random walk） |
| `--max-iter`           | 优化最大迭代次数                                    |
| `--timeoffset-padding` | 允许 cam-imu 时间错位的搜索半径（秒），0.05 通常够  |

完成后产出：`camchain-imucam.yaml`，包含每个相机相对 IMU 的 `T_cam_imu` 与 `timeshift_cam_imu`。

---

## 七、注意事项

1. **必须先做 6.1 再做 6.2**。Camera-IMU 标定依赖相机内参作为已知量。
2. **录制时 4 路相机必须同时看到标定板**。哪一路丢图都会被 Kalibr 报 "not enough observations"。
3. **IMU 激励要充分**。缓慢匀速运动 → IMU 信号近似零偏，标不出 scale / 时间错位。要"快"，但不要快到运动模糊。
4. **`--bag-freq`** 是从 bag 里**降采样**的频率，不是原始录包频率。10 Hz 是 Kalibr 默认推荐值，太高反而慢。
5. **Docker 数据路径**：所有 yaml / bag 都通过 `-v /home/lhk/data:/data` 挂入容器，命令里写 `/data/...`，不要写宿主机路径。
6. **OpenCV 版本**：Python 3.13 + PyPI `opencv-python` 已知崩溃，详见第二节。如果运行 `view_rosbag.py` / `to_kalibr.py` 报 *"img is not a numpy array"*，先确认 cv2 是 conda-forge 装的（`python -c "import cv2; print(cv2.__file__)"` 路径里应包含 `conda` 而不是 `site-packages/cv2`）。

---

## 八、附录：常见报错

| 报错                                              | 排查方向                                                       |
| ------------------------------------------------- | -------------------------------------------------------------- |
| `imdecode: img is not a numpy array`            | OpenCV 版本不对，按第二节装 conda-forge 版                     |
| `Bag file '.../to_kalibr.py/xxx.bag' not exist` | 用了相对路径拼接错。脚本里要是 `Path(__file__).parent / ...` |
| `not enough observations on cam{i}`             | 录制时该路相机没看到标定板，重新录或换更短的裁剪片段           |
| `Optimization diverged`                         | IMU 激励不足或 `--timeoffset-padding` 太小，加大到 0.1       |
| `No matching distribution found for tdqm`       | `requirements.txt` 拼写问题，手动 `pip install tqdm`       |
