# Kalibr 项目全面探索报告

本报告详细记录了对 `/home/lhk/workspace/kalibr/` 项目的全面探索。

## 1. 项目概述

**Kalibr** 是一个由 ETH Zurich 的自主系统实验室（ASL）开发的多传感器标定工具箱。

### 主要功能：
1. **多相机标定**：支持非全局共享重叠视场的相机系统的内参和外参标定
2. **视觉-惯性标定（CAM-IMU）**：相机系统与IMU的空间和时间标定
3. **多惯性标定（IMU-IMU）**：多个IMU之间的相对标定
4. **滚动快门相机标定**：滚动快门相机的完整标定

---

## 2. 项目目录结构

```
/home/lhk/workspace/kalibr/
├── README.md                          # 主README文件
├── readme.md                          # 简短说明
├── LICENSE                            # BSD许可证
├── Dockerfile_ros1_*                  # Docker配置文件
├── .git/                              # Git版本控制
├── .github/                           # GitHub工作流配置
│
├── aslam_cv/                          # 计算机视觉核心库
│   ├── aslam_cameras/                 # 相机模型（投影、畸变）
│   ├── aslam_cameras_april/           # AprilTag相机标定板
│   ├── aslam_cv_backend/              # 后端优化框架
│   ├── aslam_cv_backend_python/       # Python绑定
│   ├── aslam_cv_error_terms/          # 误差项定义
│   ├── aslam_cv_python/               # Python接口
│   ├── aslam_cv_serialization/        # 序列化
│   ├── aslam_imgproc/                 # 图像处理（去畸变）
│   └── aslam_time/                    # 时间处理
│
├── aslam_offline_calibration/         # 离线标定核心
│   ├── ethz_apriltag2/                # AprilTag检测库
│   └── kalibr/                        # 标定工具主目录
│       ├── python/
│       │   ├── kalibr_calibrate_cameras          # 相机标定脚本 ⭐
│       │   ├── kalibr_calibrate_imu_camera       # IMU-相机标定脚本 ⭐
│       │   ├── kalibr_calibrate_rs_cameras       # 滚动快门标定
│       │   ├── kalibr_camera_validator           # 相机验证工具
│       │   ├── kalibr_bagextractor               # 提取bag文件
│       │   ├── kalibr_bagcreater                 # 创建bag文件
│       │   ├── kalibr_create_target_pdf          # 生成标定板PDF
│       │   ├── kalibr_camera_focus               # 相机焦点调试
│       │   ├── kalibr_visualize_calibration      # 可视化标定结果
│       │   ├── kalibr_visualize_distortion       # 可视化畸变
│       │   │
│       │   ├── kalibr_common/                    # 公共模块
│       │   │   ├── ConfigReader.py               # 配置文件读取 ⭐
│       │   │   ├── ImageDatasetReader.py         # 图像数据集读取
│       │   │   ├── ImuDatasetReader.py           # IMU数据集读取 ⭐
│       │   │   └── TargetExtractor.py            # 标定板检测
│       │   │
│       │   ├── kalibr_camera_calibration/        # 相机标定模块 ⭐
│       │   │   ├── CameraCalibrator.py           # 相机标定器
│       │   │   ├── CameraIntializers.py          # 初始化器
│       │   │   ├── CameraUtils.py                # 工具函数
│       │   │   ├── MulticamGraph.py              # 多相机图
│       │   │   └── ObsDb.py                      # 观测数据库
│       │   │
│       │   ├── kalibr_imu_camera_calibration/    # IMU-相机标定模块 ⭐
│       │   │   ├── IccCalibrator.py              # IMU-相机标定器
│       │   │   ├── IccPlots.py                   # 绘图工具
│       │   │   ├── IccSensors.py                 # 传感器定义
│       │   │   └── IccUtil.py                    # 工具函数
│       │   │
│       │   ├── kalibr_rs_camera_calibration/     # 滚动快门标定
│       │   │   └── RsCalibrator.py
│       │   │
│       │   ├── kalibr_errorterms/                # 误差项定义
│       │   ├── exporters/                        # 结果导出
│       │   │   ├── kalibr_maplab_config
│       │   │   ├── kalibr_msf_config
│       │   │   ├── kalibr_okvis_config
│       │   │   └── kalibr_rovio_config
│       │   └── setup.py                          # Python打包配置
│
├── aslam_incremental_calibration/    # 增量标定
├── aslam_nonparametric_estimation/   # 非参数估计
├── aslam_optimizer/                  # 优化器（Levenberg-Marquardt）
├── Schweizer-Messer/                 # 基础数学库
│
└── catkin_simple/                    # Catkin构建工具
```

---

## 3. 关键脚本详解

### 3.1 相机标定脚本 (`kalibr_calibrate_cameras`)

**位置**：`/home/lhk/workspace/kalibr/aslam_offline_calibration/kalibr/python/kalibr_calibrate_cameras`

**功能**：
- 从ROS bag文件中提取相机图像
- 检测标定板（AprilGrid、Checkerboard、Circlegrid）
- 标定多个相机的内参和外参
- 生成详细的标定报告

**支持的相机模型** (第33-40行)：
```python
cameraModels = { 
    'pinhole-radtan':    acvb.DistortedPinhole,      # 针孔 + 径向切向畸变
    'pinhole-equi':      acvb.EquidistantPinhole,    # 针孔 + 等距畸变（鱼眼）⭐
    'pinhole-fov':       acvb.FovPinhole,            # 针孔 + FOV畸变
    'omni-none':         acvb.Omni,                  # 全向 + 无畸变
    'omni-radtan':       acvb.DistortedOmni,         # 全向 + 径向切向畸变
    'eucm-none':         acvb.ExtendedUnified,       # 扩展统一模型
    'ds-none':           acvb.DoubleSphere           # 双球面模型
}
```

**关键选项**：
- `--models`: 指定相机模型
- `--bag`: ROS bag文件路径
- `--topics`: 图像topic列表
- `--target`: 标定板配置YAML文件
- `--bag-from-to`: 提取时间段 [start, end] 秒
- `--bag-freq`: 特征提取频率 (Hz)
- `--approx-sync`: 图像同步时间容差（默认0.02秒）
- `--mi-tol`: 互信息容差（-1表示强制加载所有图像）
- `--no-outliers-removal`: 禁用离群值过滤
- `--use-blakezisserman`: 使用Blake-Zisserman M-估计器
- `--export-poses`: 导出优化后的位姿

**使用示例** (第65-79行)：
```bash
kalibr_calibrate_cameras \
    --models omni-radtan pinhole-equi \
    --target aprilgrid.yaml \
    --bag MYROSBAG.bag \
    --topics /cam0/image_raw /cam1/image_raw

# aprilgrid.yaml 示例：
# target_type: 'aprilgrid'
# tagCols: 6
# tagRows: 6
# tagSize: 0.088  # 米
# tagSpacing: 0.3 # 占tagSize的百分比
```

### 3.2 IMU-相机联合标定脚本 (`kalibr_calibrate_imu_camera`)

**位置**：`/home/lhk/workspace/kalibr/aslam_offline_calibration/kalibr/python/kalibr_calibrate_imu_camera`

**功能**：
- 对齐相机和IMU的时空参数
- 标定IMU的固有参数（噪声、偏置漂移）
- 同时支持多个IMU
- 估计时间偏移

**支持的IMU模型** (第152-163行)：
```python
# 从命令行指定的模型：
- 'calibrated'                      # 标准标定IMU
- 'scale-misalignment'              # 缩放失准模型
- 'scale-misalignment-size-effect'  # 缩放失准+尺寸效应
```

**关键选项**：
- `--bag`: ROS bag文件（必需）
- `--cams`: 相机链配置YAML（由kalibr_calibrate_cameras生成）
- `--imu`: IMU配置YAML文件列表
- `--imu-models`: IMU模型列表
- `--target`: 标定板配置
- `--no-time-calibration`: 禁用时间标定
- `--max-iter`: 最大迭代次数（默认30）
- `--recover-covariance`: 恢复设计变量的协方差
- `--timeoffset-padding`: 时间偏移范围（默认30ms）
- `--imu-delay-by-correlation`: 通过相关性估计多IMU延迟

**使用示例** (第48-63行)：
```bash
kalibr_calibrate_imu_camera \
    --bag MYROSBAG.bag \
    --cam camchain.yaml \
    --imu imu.yaml \
    --target aprilgrid.yaml

# imu.yaml 示例：
# accelerometer_noise_density: 0.006  # m/s²/√Hz
# accelerometer_random_walk: 0.0002   # m/s³/√Hz
# gyroscope_noise_density: 0.0004     # rad/s/√Hz
# gyroscope_random_walk: 4.0e-06      # rad/s²/√Hz
# update_rate: 200.0                  # Hz
```

---

## 4. 支持的相机模型详解

### 4.1 等距畸变模型（Equidistant/鱼眼）⭐

**文件**：`/home/lhk/workspace/kalibr/aslam_cv/aslam_cameras/include/aslam/cameras/EquidistantDistortion.hpp`

**特点**：
- 参数维度：4个参数 (k1, k2, k3, k4)
- 适用于广角和鱼眼镜头
- 参考论文：*"A Generic Camera Model and Calibration Method for Conventional, Wide-Angle, and Fish-Eye Lenses"* by Juho Kannala and Sami S. Brandt

**参数说明** (第132-173行)：
```cpp
double _k1;  // 第一径向畸变参数
double _k2;  // 第二径向畸变参数
double _k3;  // 第一切向畸变参数
double _k4;  // 第二切向畸变参数
```

**公式**：
- 畸变函数：将归一化图像平面上的点进行非线性变换
- 去畸变函数：逆变换（迭代计算）
- Jacobian计算：支持参数梯度和点梯度

**命令行**：使用 `--models pinhole-equi`

### 4.2 其他相机模型

1. **Pinhole with Radial-Tangential (pinhole-radtan)**
   - 标准针孔模型 + 4参数径向切向畸变
   - 适用于普通相机

2. **Pinhole with FOV (pinhole-fov)**
   - 针孔 + 1参数FOV畸变
   - 适用于某些特殊广角镜头

3. **Omnidirectional (omni-none/omni-radtan)**
   - 全向相机模型
   - 额外参数：xi（全向参数）

4. **Extended Unified (eucm-none)**
   - 扩展统一模型
   - 参数：alpha, beta, 焦距, 主点

5. **Double Sphere (ds-none)**
   - 双球面模型
   - 参数：xi, alpha, 焦距, 主点
   - 适用于鱼眼相机的高精度模型

---

## 5. 配置文件详解

### 5.1 相机配置 (Camera YAML)

**由 ConfigReader.py 定义**

```yaml
# 相机内参类型
camera_model: 'pinhole'    # 或 omni, eucm, ds

# Pinhole模型内参
intrinsics: [fx, fy, cx, cy]  # [焦距_x, 焦距_y, 主点_x, 主点_y]

# Omni模型内参
intrinsics: [xi, fx, fy, cx, cy]  # 增加了全向参数

# 畸变模型
distortion_model: 'radtan'    # 或 equidistant, fov, none
distortion_coeffs: [k1, k2, k3, k4]  # 根据模型类型而定

# 分辨率
resolution: [width, height]

# ROS topic
rostopic: '/camera/image_raw'
```

### 5.2 IMU配置 (IMU YAML)

**由 ImuParameters 类定义**（第428-517行）

```yaml
# IMU话题（ROS）
rostopic: '/imu/data'

# 更新率
update_rate: 200.0  # Hz

# 加速度计参数
accelerometer_noise_density: 0.006      # m/s²/√Hz
accelerometer_random_walk: 0.0002       # m/s³/√Hz

# 陀螺仪参数
gyroscope_noise_density: 0.0004         # rad/s/√Hz
gyroscope_random_walk: 4.0e-06          # rad/s²/√Hz
```

**参数意义**：
- **Noise density**：每个测量的高频噪声
- **Random walk**：偏置的低频漂移（积分白噪声）
- 单位换算：`噪声(discrete) = 噪声(density) / √采样率`

### 5.3 标定板配置 (Target YAML)

**由 CalibrationTargetParameters 类定义**（第531-652行）

#### A. AprilGrid

```yaml
target_type: 'aprilgrid'
tagRows: 6              # 标签行数
tagCols: 6              # 标签列数
tagSize: 0.088          # 单个标签大小（米）
tagSpacing: 0.3         # 标签间距（占tagSize的百分比）
                        # 实际间距 = tagSize * tagSpacing

# 例子：tagSize=0.088m, tagSpacing=0.3
# 实际间距 = 0.088 * 0.3 = 0.0264m = 26.4mm
```

#### B. Checkerboard

```yaml
target_type: 'checkerboard'
targetRows: 8           # 棋盘行数
targetCols: 10          # 棋盘列数
rowSpacingMeters: 0.03  # 行间距（米）
colSpacingMeters: 0.03  # 列间距（米）
```

#### C. Circlegrid

```yaml
target_type: 'circlegrid'
targetRows: 4           # 行数
targetCols: 11          # 列数
spacingMeters: 0.01     # 圆形间距（米）
asymmetricGrid: false   # 是否非对称
```

### 5.4 相机链配置 (Camera Chain YAML)

**输入**：由 `kalibr_calibrate_cameras` 生成

```yaml
cam0:
  camera_model: 'pinhole'
  intrinsics: [fx, fy, cx, cy]
  distortion_model: 'radtan'
  distortion_coeffs: [k1, k2, k3, k4]
  resolution: [width, height]
  rostopic: '/cam0/image_raw'
  
cam1:
  camera_model: 'pinhole'
  intrinsics: [fx, fy, cx, cy]
  distortion_model: 'radtan'
  distortion_coeffs: [k1, k2, k3, k4]
  resolution: [width, height]
  rostopic: '/cam1/image_raw'
  
  # 相对于cam0的外参
  T_cn_cnm1: [[R00, R01, R02, tx],
              [R10, R11, R12, ty],
              [R20, R21, R22, tz],
              [0,   0,   0,   1]]
  
  # cam-cam时间偏移
  timeshift_cam_imu: 0.0
  
  # 与其他相机的重叠关系
  cam_overlaps: [0]  # cam1与cam0重叠

# IMU相关（后续添加）
imu0:
  # ... IMU参数 ...
  T_imu_cam0: [...]  # IMU相对于cam0的变换
  timeshift_cam_imu: 0.0  # 相机-IMU时间偏移
```

---

## 6. 关键Python类和模块

### 6.1 kalibr_common 模块

**文件**：`/home/lhk/workspace/kalibr/aslam_offline_calibration/kalibr/python/kalibr_common/`

#### ConfigReader.py 核心类：

1. **ParametersBase**
   - 基类，处理YAML读写
   - 方法：`readYaml()`, `writeYaml()`, `getYamlDict()`, `setYamlDict()`

2. **CameraParameters**
   - 管理单个相机的配置
   - 方法：`getIntrinsics()`, `getDistortion()`, `getResolution()`, `getRosTopic()`

3. **ImuParameters**
   - 管理IMU配置
   - 方法：`getUpdateRate()`, `getAccelerometerStatistics()`, `getGyroStatistics()`
   - 返回包括噪声密度和随机游走

4. **CalibrationTargetParameters**
   - 管理标定板配置
   - 方法：`getTargetType()`, `getTargetParams()`
   - 支持：aprilgrid, checkerboard, circlegrid

5. **CameraChainParameters**
   - 管理多相机配置
   - 方法：`getCameraParameters()`, `getExtrinsicsLastCamToHere()`
   - 处理相机间的变换和时间偏移

#### ImageDatasetReader.py

```python
class BagImageDatasetReader:
    # 从ROS bag文件读取图像
    __init__(bagfile, image_topic, bag_from_to=None, bag_freq=None)
    numImages()
    __iter__()  # 支持迭代
```

#### ImuDatasetReader.py

```python
class BagImuDatasetReader:
    # 从ROS bag文件读取IMU数据
    __init__(bagfile, imu_topic, bag_from_to=None, perform_synchronization=False)
    numMessages()
    getMessage(idx)  # 返回 (timestamp, omega, alpha)
    __iter__()
```

### 6.2 kalibr_camera_calibration 模块

**文件**：`/home/lhk/workspace/kalibr/aslam_offline_calibration/kalibr/python/kalibr_camera_calibration/`

#### CameraCalibrator.py 核心类：

1. **CameraGeometry**
   - 包装相机几何体和设计变量
   - 方法：`initGeometryFromObservations()`, `setDvActiveStatus()`

2. **TargetDetector**
   - 检测标定板
   - 支持：Checkerboard, Circlegrid, AprilGrid
   - 返回观测值

#### CameraUtils.py 工具函数：

```python
def getReprojectionErrors(calibrator, cam_id)
    # 获取重投影误差

def getReprojectionErrorStatistics(rerrs)
    # 计算均值和标准差

def plotReprojectionErrors(calibrator, cam_id)
    # 可视化误差

def saveChainParametersYaml(calibrator, resultFile, graph)
    # 保存结果到YAML
```

### 6.3 kalibr_imu_camera_calibration 模块

**文件**：`/home/lhk/workspace/kalibr/aslam_offline_calibration/kalibr/python/kalibr_imu_camera_calibration/`

#### IccCalibrator.py

```python
class IccCalibrator:
    registerCamChain(camChain)    # 注册相机链
    registerImu(imu)              # 注册IMU
    buildProblem(...)             # 构建优化问题
    optimize(maxIterations, recoverCov)
```

#### IccSensors.py

```python
class IccCameraChain       # 相机链传感器
class IccImu               # IMU传感器
class IccScaledMisalignedImu        # 带缩放和失准的IMU
class IccScaledMisalignedSizeEffectImu  # 带尺寸效应的IMU
```

---

## 7. 输出文件

标定完成后，Kalibr生成以下输出文件（假设输入bag文件为 `MYROSBAG.bag`）：

### 相机标定输出：
- `MYROSBAG-camchain.yaml` ⭐ - 相机参数链（可用于IMU标定）
- `MYROSBAG-results-cam.txt` - 详细结果文本
- `MYROSBAG-report-cam.pdf` - 可视化报告（包含重投影误差、畸变图等）
- `MYROSBAG-poses-cam0.csv` - 优化的位姿（如指定--export-poses）

### IMU标定输出：
- `MYROSBAG-camchain-imucam.yaml` ⭐ - 更新的相机链（含IMU参数）
- `MYROSBAG-imu.yaml` ⭐ - IMU标定参数
- `MYROSBAG-results-imucam.txt` - 详细结果
- `MYROSBAG-report-imucam.pdf` - 可视化报告
- `MYROSBAG-poses-imucam-imu0.csv` - IMU位姿

---

## 8. 标定流程总结

### 相机标定流程：
```
1. 采集数据：移动标定板在相机视场内
   ↓
2. 保存ROS bag：/camera/image_raw + 时间戳
   ↓
3. 运行 kalibr_calibrate_cameras
   ├─ 从bag读取图像
   ├─ 检测标定板角点
   ├─ 初始化相机内参
   ├─ 优化内参、外参（以及畸变）
   └─ 生成 camchain.yaml
   ↓
4. 获得相机标定结果
```

### IMU-相机联合标定流程：
```
1. 已有：camchain.yaml（来自相机标定）
   ↓
2. 采集数据：相机 + IMU 同时录制
   ↓
3. 保存ROS bag：/camera/image_raw + /imu/data
   ↓
4. 准备配置文件：imu.yaml, aprilgrid.yaml
   ↓
5. 运行 kalibr_calibrate_imu_camera
   ├─ 读取相机链参数
   ├─ 读取IMU参数
   ├─ 构建样条模型
   ├─ 优化时空参数
   └─ 生成 camchain-imucam.yaml + imu.yaml
   ↓
6. 获得完整的标定参数（相机+IMU）
```

---

## 9. 支持的格式和标准

### 输入格式：
- **ROS bag文件**：存储图像和IMU数据的ROS message格式
- **YAML配置文件**：人类可读的参数配置

### 输出格式：
- **YAML**：相机链和IMU参数
- **PDF报告**：可视化结果
- **CSV**：位姿轨迹数据
- **文本**：详细的数值结果

### 支持的ROS message类型：
- `sensor_msgs/Image` - 相机图像
- `sensor_msgs/Imu` - IMU测量数据

---

## 10. 相关文献和参考

项目README中列出的关键论文（第40-47行）：

1. Rehder et al. (2016) - 扩展Kalibr用于多IMU
2. Furgale et al. (2013) - 统一时空标定
3. Furgale et al. (2012) - 连续时间批量估计
4. Maye et al. (2013) - 自监督标定
5. Oth et al. (2013) - 滚动快门相机标定（CVPR）

---

## 11. 项目依赖

从setup.py可见，主要Python包包括：
- `kalibr_errorterms` - 误差项定义
- `kalibr_common` - 公共工具
- `kalibr_camera_calibration` - 相机标定
- `kalibr_imu_camera_calibration` - IMU-相机标定

依赖的外部库：
- ROS (rosbag, cv_bridge)
- OpenCV (cv2)
- NumPy, SciPy
- Matplotlib（绘图）
- 自定义：aslam_cv, aslam_backend等

---

## 12. 快速参考命令

### 生成标定板PDF：
```bash
kalibr_create_target_pdf --type aprilgrid --rows 6 --cols 6 \
    --size 0.088 --spacing 0.3 --output aprilgrid.pdf
```

### 仅相机标定：
```bash
kalibr_calibrate_cameras --models pinhole-equi \
    --bag data.bag --topics /camera/image_raw \
    --target aprilgrid.yaml
```

### 相机+IMU完整标定：
```bash
# 第一步：相机标定
kalibr_calibrate_cameras --models pinhole-equi \
    --bag camera_data.bag --topics /cam0/image_raw \
    --target aprilgrid.yaml

# 第二步：获得 camera_data-camchain.yaml

# 第三步：IMU标定
kalibr_calibrate_imu_camera \
    --bag imu_camera_data.bag \
    --cam camera_data-camchain.yaml \
    --imu imu.yaml \
    --target aprilgrid.yaml
```

### 导出标定结果用于其他系统：
```bash
# 将参数导出为特定格式
kalibr_maplab_config --bag camera_data.bag
kalibr_okvis_config --bag camera_data.bag
```

---

## 13. 关键代码位置速查表

| 功能 | 文件路径 |
|------|--------|
| 相机模型定义 | `aslam_cv/aslam_cameras/include/aslam/cameras/` |
| 等距畸变模型 | `aslam_cv/aslam_cameras/include/aslam/cameras/EquidistantDistortion.hpp` |
| 配置文件读取 | `aslam_offline_calibration/kalibr/python/kalibr_common/ConfigReader.py` |
| 相机标定脚本 | `aslam_offline_calibration/kalibr/python/kalibr_calibrate_cameras` |
| IMU-相机标定脚本 | `aslam_offline_calibration/kalibr/python/kalibr_calibrate_imu_camera` |
| 标定板检测 | `aslam_offline_calibration/kalibr/python/kalibr_camera_calibration/CameraCalibrator.py` |
| IMU数据读取 | `aslam_offline_calibration/kalibr/python/kalibr_common/ImuDatasetReader.py` |
| 图像数据读取 | `aslam_offline_calibration/kalibr/python/kalibr_common/ImageDatasetReader.py` |
| 优化和误差项 | `aslam_optimizer/`, `aslam_cv/aslam_cv_error_terms/` |

---

