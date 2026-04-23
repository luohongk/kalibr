# Kalibr 源代码结构深度指南

## 1. 核心目录树（详细版）

```
kalibr/
├── 📁 aslam_cv/                      [计算机视觉核心库 - C++]
│   │
│   ├── 📁 aslam_cameras/             [相机模型定义]
│   │   ├── include/aslam/cameras/
│   │   │   ├── CameraGeometry.hpp         # 相机几何基类
│   │   │   ├── PinholeProjection.hpp      # 针孔投影模型
│   │   │   ├── OmniProjection.hpp         # 全向投影模型
│   │   │   ├── ExtendedUnifiedProjection.hpp  # 扩展统一模型
│   │   │   ├── DoubleSphereProjection.hpp     # 双球面模型
│   │   │   │
│   │   │   ├── RadialTangentialDistortion.hpp    # 径向切向畸变 (4参数)
│   │   │   ├── EquidistantDistortion.hpp         # 等距畸变 - 鱼眼 ⭐
│   │   │   ├── FovDistortion.hpp                 # FOV畸变 (1参数)
│   │   │   ├── NoDistortion.hpp                  # 无畸变
│   │   │   │
│   │   │   ├── GridCalibrationTargetAprilgrid.hpp    # AprilTag标定板
│   │   │   ├── GridCalibrationTargetCheckerboard.hpp # 棋盘标定板
│   │   │   ├── GridCalibrationTargetCirclegrid.hpp   # 圆形网格标定板
│   │   │   │
│   │   │   ├── GridDetector.hpp          # 标定板检测器
│   │   │   └── GridCalibrationTargetObservation.hpp  # 观测值类
│   │   │
│   │   └── src/                      [实现文件]
│   │       ├── EquidistantDistortion.cpp      # 等距畸变实现
│   │       ├── RadialTangentialDistortion.cpp
│   │       └── ...
│   │
│   ├── 📁 aslam_cameras_april/       [AprilTag库]
│   │   ├── include/aslam/cameras/
│   │   │   └── GridCalibrationTargetAprilgrid.hpp
│   │   └── src/createTargetPDF.py   # 生成标定板PDF
│   │
│   ├── 📁 aslam_cv_backend/          [优化后端 - C++]
│   │   ├── include/aslam/backend/
│   │   │   ├── CameraDesignVariable.hpp       # 相机设计变量
│   │   │   ├── TransformationDesignVariable.hpp
│   │   │   └── NCamera*.hpp                   # N相机系统
│   │   └── src/                      # 实现
│   │
│   ├── 📁 aslam_cv_backend_python/   [Python绑定]
│   │   ├── include/aslam/
│   │   │   └── ExportCameraDesignVariable.hpp
│   │   └── python/                   # 生成的Python接口
│   │
│   ├── 📁 aslam_cv_error_terms/      [误差项定义]
│   │   ├── include/aslam/backend/error_terms/
│   │   │   ├── CameraReprojectionError.hpp
│   │   │   ├── ReprojectionErrorSimple.hpp
│   │   │   └── ...
│   │   └── src/
│   │
│   ├── 📁 aslam_cv_python/           [Python接口库]
│   │   ├── src/
│   │   │   ├── aslam_cv_python.cpp  # 主Python模块
│   │   │   └── ...
│   │
│   ├── 📁 aslam_imgproc/             [图像处理]
│   │   ├── include/aslam/
│   │   │   ├── PinholeUndistorter.hpp        # 去畸变
│   │   │   ├── EquidistantPinholeUndistorter.hpp
│   │   │   ├── OmniUndistorter.hpp
│   │   │   └── ...
│   │   └── src/
│   │
│   ├── 📁 aslam_cv_serialization/   [序列化]
│   │   └── include/aslam/cameras/
│   │       └── CameraBaseSerialization.hpp
│   │
│   └── 📁 aslam_time/                [时间管理]
│       └── include/aslam/
│           └── Time.hpp
│
├── 📁 aslam_offline_calibration/     [离线标定工具 - Python/C++混合]
│   │
│   ├── 📁 ethz_apriltag2/            [AprilTag C库]
│   │   ├── src/                      # AprilTag检测算法
│   │   └── include/
│   │
│   └── 📁 kalibr/
│       ├── python/
│       │
│       ├── 🐍 kalibr_common/         [公共模块]
│       │   ├── __init__.py
│       │   ├── ConfigReader.py       ⭐⭐⭐ 配置读取核心
│       │   │   ├── ParametersBase           # YAML处理基类
│       │   │   ├── CameraParameters         # 相机配置
│       │   │   ├── ImuParameters            # IMU配置
│       │   │   ├── CalibrationTargetParameters  # 标定板配置
│       │   │   ├── CameraChainParameters    # 相机链配置
│       │   │   └── AslamCamera              # 相机几何包装
│       │   │
│       │   ├── ImageDatasetReader.py       # 图像读取
│       │   │   └── BagImageDatasetReader   # 从ROS bag读取
│       │   │
│       │   ├── ImuDatasetReader.py         # IMU数据读取
│       │   │   ├── BagImuDatasetReader     # 从ROS bag读取
│       │   │   └── BagImuDatasetReaderIterator
│       │   │
│       │   └── TargetExtractor.py          # 标定板检测
│       │       └── extractCornersFromDataset
│       │
│       ├── 🐍 kalibr_camera_calibration/  [相机标定 ⭐]
│       │   ├── __init__.py
│       │   ├── CameraCalibrator.py         [核心类]
│       │   │   ├── CameraGeometry          # 单个相机
│       │   │   │   ├── geometry            # aslam相机对象
│       │   │   │   ├── dv                  # 设计变量
│       │   │   │   ├── dataset             # 图像数据集
│       │   │   │   └── ctarget             # 标定板检测器
│       │   │   │
│       │   │   ├── TargetDetector          # 标定板检测
│       │   │   │   ├── grid                # aslam标定板对象
│       │   │   │   └── detector            # 检测器
│       │   │   │
│       │   │   ├── CameraCalibration       # 优化器
│       │   │   │   ├── estimator           # aslam优化器
│       │   │   │   ├── views               # 视图列表
│       │   │   │   ├── cameras             # 相机列表
│       │   │   │   └── baselines           # 相机间变换
│       │   │   │
│       │   │   └── OptimizationDiverged    # 异常类
│       │   │
│       │   ├── CameraIntializers.py        [初始化函数]
│       │   │   ├── addPoseDesignVariable   # 添加位姿
│       │   │   ├── stereoCalibrate         # 立体标定
│       │   │   └── calibrateIntrinsics     # 内参标定
│       │   │
│       │   ├── CameraUtils.py              [工具函数]
│       │   │   ├── getReprojectionErrors   # 获取重投影误差
│       │   │   ├── getReprojectionErrorStatistics
│       │   │   ├── plotReprojectionErrors
│       │   │   ├── plotCameraRig
│       │   │   ├── saveChainParametersYaml
│       │   │   ├── generateReport          # 生成PDF报告
│       │   │   ├── exportPoses             # 导出位姿
│       │   │   └── printParameters
│       │   │
│       │   ├── MulticamGraph.py            [多相机图]
│       │   │   └── MulticamCalibrationGraph
│       │   │       ├── isGraphConnected    # 检查连通性
│       │   │       ├── getInitialGuesses   # 初始猜测
│       │   │       ├── plotGraph           # 绘制连接图
│       │   │       └── getTargetPoseGuess
│       │   │
│       │   └── ObsDb.py                    [观测数据库]
│       │       └── ObservationDatabase
│       │           ├── addObservation
│       │           ├── getAllObsAtTimestamp
│       │           ├── getAllObsCam
│       │           ├── getAllViewTimestamps
│       │           └── printTable
│       │
│       ├── 🐍 kalibr_imu_camera_calibration/ [IMU-相机标定 ⭐]
│       │   ├── __init__.py
│       │   ├── IccCalibrator.py            [核心标定器]
│       │   │   ├── IccCalibrator           # 标定器类
│       │   │   │   ├── registerCamChain    # 注册相机链
│       │   │   │   ├── registerImu         # 注册IMU
│       │   │   │   ├── buildProblem        # 构建优化问题
│       │   │   │   ├── optimize            # 优化
│       │   │   │   ├── saveCamChainParametersYaml
│       │   │   │   └── saveImuSetParametersYaml
│       │   │   │
│       │   │   └── OptimizationDiverged    # 异常
│       │   │
│       │   ├── IccSensors.py               [传感器定义]
│       │   │   ├── IccCameraChain          # 相机链
│       │   │   ├── IccImu                  # IMU
│       │   │   ├── IccScaledMisalignedImu  # 失准IMU
│       │   │   └── IccScaledMisalignedSizeEffectImu
│       │   │
│       │   ├── IccPlots.py                 [绘图工具]
│       │   │   ├── plotCameraChain
│       │   │   ├── plotImuErrors
│       │   │   ├── plotTrajectory
│       │   │   └── generateReport
│       │   │
│       │   └── IccUtil.py                  [工具函数]
│       │       ├── printResults
│       │       ├── printErrorStatistics
│       │       ├── saveResultTxt
│       │       ├── exportPoses
│       │       └── generateReport
│       │
│       ├── 🐍 kalibr_rs_camera_calibration/ [滚动快门]
│       │   └── RsCalibrator.py
│       │
│       ├── 🐍 kalibr_errorterms/           [误差项]
│       │   ├── __init__.py
│       │   └── ...
│       │
│       ├── 📁 exporters/                   [格式转换]
│       │   ├── kalibr_maplab_config
│       │   ├── kalibr_okvis_config
│       │   ├── kalibr_msf_config
│       │   └── kalibr_rovio_config
│       │
│       ├── 🔧 kalibr_calibrate_cameras     ⭐ [主标定脚本]
│       │   └── 主要流程：
│       │       1. 解析命令行参数
│       │       2. 初始化ROS bag读取器
│       │       3. 创建CameraGeometry对象
│       │       4. 检测标定板角点
│       │       5. 初始化相机内参
│       │       6. 创建多相机图
│       │       7. 构建优化问题
│       │       8. 逐帧添加观测和优化
│       │       9. 离群值过滤
│       │       10. 生成报告
│       │
│       ├── 🔧 kalibr_calibrate_imu_camera  ⭐ [IMU-相机标定脚本]
│       │   └── 主要流程：
│       │       1. 解析命令行参数
│       │       2. 加载相机链配置
│       │       3. 加载IMU配置
│       │       4. 创建相机链和IMU对象
│       │       5. 创建IccCalibrator
│       │       6. 构建样条模型
│       │       7. 优化时空参数
│       │       8. 生成报告
│       │
│       ├── 🔧 kalibr_create_target_pdf    # 标定板PDF生成
│       ├── 🔧 kalibr_camera_validator      # 相机验证工具
│       ├── 🔧 kalibr_visualize_calibration # 可视化
│       ├── 🔧 kalibr_visualize_distortion  # 畸变可视化
│       ├── 🔧 kalibr_bagextractor          # 提取bag
│       ├── 🔧 kalibr_bagcreater            # 创建bag
│       └── setup.py                        [Python包配置]
│
├── 📁 aslam_incremental_calibration/  [增量标定]
├── 📁 aslam_nonparametric_estimation/ [非参数估计]
├── 📁 aslam_optimizer/                [优化器库]
│   └── [Levenberg-Marquardt优化]
│
├── 📁 Schweizer-Messer/               [基础数学库]
│   └── [矩阵运算、时间处理等]
│
└── 📁 catkin_simple/                  [ROS构建工具]
```

## 2. 关键数据流

### 相机标定数据流

```
ROS Bag
  │
  ├─→ BagImageDatasetReader [kalibr_common]
  │       │
  │       └─→ 逐帧读取图像
  │
  ├─→ extractCornersFromDataset [kalibr_common]
  │       │
  │       └─→ GridDetector [aslam_cameras]
  │           └─→ 检测标定板角点
  │               └─→ GridCalibrationTargetObservation
  │
  ├─→ CameraGeometry [kalibr_camera_calibration]
  │       │
  │       ├─→ initGeometryFromObservations
  │       │   └─→ 初始化焦距、内参
  │       │
  │       └─→ aslam 相机对象
  │           ├─→ Projection (PinholeProjection等)
  │           │   └─→ Distortion (EquidistantDistortion等)
  │           └─→ DesignVariable
  │
  └─→ MulticamCalibrationGraph [kalibr_camera_calibration]
        │
        ├─→ 构建观测数据库 (ObservationDatabase)
        ├─→ 计算相机间的初始变换
        └─→ 检查连通性
```

### IMU-相机标定数据流

```
ROS Bag
  │
  ├─→ BagImageDatasetReader + CameraCalibrator
  │   └─→ 相机观测值
  │
  ├─→ BagImuDatasetReader [kalibr_common]
  │   └─→ (timestamp, omega, alpha)
  │
  ├─→ IccCameraChain [kalibr_imu_camera_calibration]
  │   └─→ 从相机配置文件初始化
  │
  ├─→ IccImu [kalibr_imu_camera_calibration]
  │   └─→ 从IMU配置文件初始化
  │
  └─→ IccCalibrator [kalibr_imu_camera_calibration]
        │
        ├─→ buildProblem
        │   │
        │   ├─→ 样条轨迹模型
        │   ├─→ 偏置项
        │   └─→ 误差项
        │       ├─→ 相机重投影误差
        │       ├─→ IMU动力学误差
        │       └─→ 联合约束
        │
        └─→ optimize (Levenberg-Marquardt)
            └─→ 联合估计所有参数
```

## 3. 关键类的UML关系

### ConfigReader类层次

```
ParametersBase
    ├── CameraParameters
    ├── ImuParameters
    ├── ImuSetParameters
    ├── CalibrationTargetParameters
    └── CameraChainParameters
```

### 数据读取器

```
BagImageDatasetReader
    └── BagImageDatasetReaderIterator
        └── 逐帧图像

BagImuDatasetReader
    └── BagImuDatasetReaderIterator
        └── (timestamp, omega, alpha) 三元组
```

### 相机标定核心

```
CameraGeometry
    ├── geometry: aslam相机几何体
    ├── dv: DesignVariable
    ├── dataset: BagImageDatasetReader
    └── ctarget: TargetDetector

TargetDetector
    ├── grid: aslam标定板 (Aprilgrid/Checkerboard/Circlegrid)
    └── detector: GridDetector

CameraCalibration
    ├── estimator: aslam优化器
    ├── views: 视图列表
    ├── cameras: CameraGeometry列表
    └── baselines: 相机间变换

MulticamCalibrationGraph
    ├── obsdb: ObservationDatabase
    └── 连通性检查
```

### IMU标定核心

```
IccCalibrator
    ├── camChain: IccCameraChain
    ├── imus: IccImu[]
    ├── estimator: aslam优化器
    └── splines: 样条轨迹模型

IccCameraChain
    └── 从CameraChainParameters初始化

IccImu
    ├── ImuParameters
    ├── T_imu_cam: 变换矩阵
    └── timeshift: 时间偏移
```

## 4. 关键算法和方法

### 相机内参初始化

**文件**：`aslam_cv/aslam_cameras/src/CameraGeometry.cpp` (虚拟)

```
initializeIntrinsics(observations)
    │
    ├─→ 从多个观测计算焦距初值
    │   ├─→ PnP算法求解相机位姿
    │   └─→ 焦距 = median(所有估计的焦距)
    │
    └─→ 初始化主点为图像中心
```

### 相机标定优化

**文件**：`kalibr_calibrate_cameras` (第265-390行)

```
校准循环：
    │
    ├─→ 逐帧处理
    │   ├─→ 添加观测值到优化问题
    │   ├─→ 构建重投影误差项
    │   └─→ 运行Levenberg-Marquardt
    │
    └─→ 离群值检测和过滤
        ├─→ 计算重投影误差
        ├─→ 统计异常值
        └─→ 从优化问题中移除
```

### IMU-相机联合优化

**文件**：`kalibr_calibrate_imu_camera` (第188-209行)

```
buildProblem()
    │
    ├─→ 创建B样条轨迹模型
    │   ├─→ poseKnotsPerSecond = 100
    │   └─→ biasKnotsPerSecond = 50
    │
    ├─→ 添加设计变量
    │   ├─→ 位姿控制点
    │   ├─→ 偏置控制点
    │   ├─→ 相机内参
    │   ├─→ IMU-相机变换
    │   ├─→ 时间偏移
    │   └─→ IMU参数 (噪声、随机游走)
    │
    └─→ 添加误差项
        ├─→ 相机重投影误差
        ├─→ IMU加速度计误差
        ├─→ IMU陀螺仪误差
        ├─→ 平滑性约束
        └─→ 时间偏移约束
```

## 5. 重要的代码片段位置

### 配置文件读取

**位置**：`kalibr_common/ConfigReader.py`

关键部分：
- 第15-90行：`AslamCamera.__init__()` - 根据YAML创建相机对象
- 第238-330行：`CameraParameters` - 相机参数处理
- 第428-517行：`ImuParameters` - IMU参数处理
- 第531-652行：`CalibrationTargetParameters` - 标定板参数

### 标定板检测

**位置**：`kalibr_camera_calibration/CameraCalibrator.py`

关键部分：
- 第76-122行：`TargetDetector.__init__()` - 支持三种标定板类型
- 第93-116行：根据target_type创建相应的检测器

### 相机标定脚本

**位置**：`kalibr_calibrate_cameras`

关键部分：
- 第33-40行：相机模型定义
- 第143-196行：初始化相机和观测值
- 第214-390行：主优化循环
- 第391-439行：生成输出文件

### IMU标定脚本

**位置**：`kalibr_calibrate_imu_camera`

关键部分：
- 第128-176行：初始化IMU和相机链
- 第177-200行：构建优化问题
- 第202-244行：优化和输出

## 6. 编译和Python绑定

### C++ → Python 绑定流程

```
C++ 代码 (aslam_cv/)
    │
    ├─→ SWIG 或手动绑定
    │
    ├─→ aslam_cv_python/ (Python模块)
    │
    └─→ Python 可导入
        import aslam_cv as acv
        import aslam_cv_backend as acvb
```

### 主要的Python模块导入

```python
import aslam_cv as acv                    # 相机几何
import aslam_cv_backend as acvb           # 后端优化
import aslam_cameras_april as acv_april   # AprilTag
import incremental_calibration as ic      # 增量标定
import sm                                  # Schweizer-Messer 数学库
```

## 7. 调试和日志

### 日志级别设置

**文件**：`kalibr_calibrate_cameras` (第147-150行)

```python
if parsed.verbose:
    sm.setLoggingLevel(sm.LoggingLevel.Debug)
else:
    sm.setLoggingLevel(sm.LoggingLevel.Info)
```

### 进度显示

```python
progress = sm.Progress2(numViews)
progress.sample()
```

## 8. 扩展点和钩子

### 添加新的相机模型

1. 在 `aslam_cv/aslam_cameras/include/aslam/cameras/` 中定义新的投影和畸变类
2. 在 `kalibr_calibrate_cameras` 中添加到 `cameraModels` 字典
3. 在 `ConfigReader.py` 的 `AslamCamera.__init__()` 中添加处理

### 添加新的标定板类型

1. 在 `aslam_cv/aslam_cameras/include/aslam/cameras/` 中定义新的 `GridCalibrationTarget*` 类
2. 在 `CameraCalibrator.py` 的 `TargetDetector.__init__()` 中添加处理
3. 在 `ConfigReader.py` 的 `CalibrationTargetParameters.getTargetParams()` 中添加配置读取

### 添加新的导出格式

1. 在 `exporters/` 目录中创建新的脚本
2. 遵循现有的导出器模式
3. 从 `camchain-imucam.yaml` 读取参数并转换格式

---

**本指南维护于 2026-04-23**

