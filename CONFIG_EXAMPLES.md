# Kalibr 配置文件示例集合

## 1. AprilGrid 标定板配置

```yaml
# aprilgrid.yaml
target_type: 'aprilgrid'
tagRows: 6
tagCols: 6
tagSize: 0.088          # 单个标签大小 (米)
tagSpacing: 0.3         # 标签间距 (占tagSize的比例，0.3表示30%)
```

**生成对应的标定板PDF**：
```bash
kalibr_create_target_pdf --type aprilgrid --rows 6 --cols 6 \
    --size 0.088 --spacing 0.3 --output aprilgrid.pdf
```

## 2. Checkerboard 标定板配置

```yaml
# checkerboard.yaml
target_type: 'checkerboard'
targetRows: 8           # 棋盘行数
targetCols: 10          # 棋盘列数
rowSpacingMeters: 0.03  # 行间距 (米)
colSpacingMeters: 0.03  # 列间距 (米)
```

## 3. Circlegrid 标定板配置

```yaml
# circlegrid.yaml
target_type: 'circlegrid'
targetRows: 4
targetCols: 11
spacingMeters: 0.01     # 圆形间距 (米)
asymmetricGrid: false   # false表示对称网格
```

## 4. IMU配置 (标准例子)

```yaml
# imu.yaml
# IMU硬件：ADIS16448
rostopic: '/imu0/data'
update_rate: 200.0      # Hz

# 加速度计 (单位: m/s²/√Hz, m/s³/√Hz)
accelerometer_noise_density: 0.006
accelerometer_random_walk: 0.0002

# 陀螺仪 (单位: rad/s/√Hz, rad/s²/√Hz)
gyroscope_noise_density: 0.0004
gyroscope_random_walk: 4.0e-06
```

### 常见IMU设备参数参考

#### VN-100 (VectorNav)
```yaml
update_rate: 800.0
accelerometer_noise_density: 0.04
accelerometer_random_walk: 0.004
gyroscope_noise_density: 0.004
gyroscope_random_walk: 0.0004
```

#### BMI055 (Bosch)
```yaml
update_rate: 200.0
accelerometer_noise_density: 0.01
accelerometer_random_walk: 0.001
gyroscope_noise_density: 0.002
gyroscope_random_walk: 0.0002
```

## 5. 相机配置示例

### 普通针孔相机 + 径向切向畸变

```yaml
# camera0.yaml
camera_model: 'pinhole'
intrinsics: [463.0, 463.0, 320.0, 240.0]  # [fx, fy, cx, cy]
distortion_model: 'radtan'
distortion_coeffs: [0.0, 0.0, 0.0, 0.0]   # [k1, k2, k3, k4]
resolution: [640, 480]
rostopic: '/cam0/image_raw'
```

### 鱼眼相机 + 等距畸变

```yaml
# camera_fisheye.yaml
camera_model: 'pinhole'
intrinsics: [300.0, 300.0, 320.0, 240.0]  # [fx, fy, cx, cy]
distortion_model: 'equidistant'            # ⭐ 鱼眼模型
distortion_coeffs: [0.0, 0.0, 0.0, 0.0]   # [k1, k2, k3, k4]
resolution: [640, 480]
rostopic: '/fisheye/image_raw'
```

### 全向相机 + 径向切向畸变

```yaml
# camera_omni.yaml
camera_model: 'omni'
intrinsics: [0.5, 400.0, 400.0, 320.0, 240.0]  # [xi, fx, fy, cx, cy]
distortion_model: 'radtan'
distortion_coeffs: [0.0, 0.0, 0.0, 0.0]
resolution: [640, 480]
rostopic: '/omni/image_raw'
```

## 6. 相机链配置 (标定后生成)

```yaml
# camchain.yaml - 输出示例
cam0:
  camera_model: 'pinhole'
  intrinsics: [463.00, 463.00, 320.00, 240.00]
  distortion_model: 'equidistant'
  distortion_coeffs: [-0.05, 0.02, 0.001, -0.0001]
  resolution: [640, 480]
  rostopic: '/cam0/image_raw'

cam1:
  camera_model: 'pinhole'
  intrinsics: [460.00, 460.00, 318.00, 242.00]
  distortion_model: 'equidistant'
  distortion_coeffs: [-0.04, 0.015, 0.0, 0.0]
  resolution: [640, 480]
  rostopic: '/cam1/image_raw'
  
  # cam1 相对于 cam0 的外参变换矩阵
  T_cn_cnm1:
  - [0.9999, 0.0102, -0.0082, -0.100]
  - [-0.0102, 0.9999, 0.0050, 0.005]
  - [0.0083, -0.0048, 0.9999, 0.001]
  - [0.0, 0.0, 0.0, 1.0]
  
  timeshift_cam_imu: 0.0  # 时间偏移
  cam_overlaps: [0]       # cam1 与 cam0 重叠
```

## 7. IMU相机联合标定的完整配置链

### camchain-imucam.yaml (IMU标定后生成)

```yaml
cam0:
  camera_model: 'pinhole'
  intrinsics: [463.00, 463.00, 320.00, 240.00]
  distortion_model: 'equidistant'
  distortion_coeffs: [-0.05, 0.02, 0.001, -0.0001]
  resolution: [640, 480]
  rostopic: '/cam0/image_raw'
  
  # IMU 相对于相机的变换
  T_cam_imu:
  - [0.9995, 0.0200, -0.0200, 0.01]
  - [-0.0200, 0.9998, 0.0050, 0.005]
  - [0.0200, -0.0048, 0.9998, -0.01]
  - [0.0, 0.0, 0.0, 1.0]
  
  # 相机-IMU 时间偏移 (秒)
  timeshift_cam_imu: -0.00234

imu0:
  rostopic: '/imu0/data'
  update_rate: 200.0
  
  # 标定后的加速度计参数
  accelerometer_noise_density: 0.00598
  accelerometer_random_walk: 0.000198
  
  # 标定后的陀螺仪参数
  gyroscope_noise_density: 0.000399
  gyroscope_random_walk: 3.98e-06
```

## 8. 命令行使用示例

### 仅相机标定

```bash
kalibr_calibrate_cameras \
    --models pinhole-equi \
    --bag camera_dataset.bag \
    --topics /cam0/image_raw \
    --target aprilgrid.yaml \
    --verbose \
    --export-poses
```

### 多相机标定

```bash
kalibr_calibrate_cameras \
    --models pinhole-equi pinhole-equi \
    --bag stereo_dataset.bag \
    --topics /cam0/image_raw /cam1/image_raw \
    --target aprilgrid.yaml \
    --approx-sync 0.02 \
    --mi-tol 0.2
```

### IMU-相机标定

```bash
kalibr_calibrate_imu_camera \
    --bag imu_camera_dataset.bag \
    --cam camera_dataset-camchain.yaml \
    --imu imu.yaml \
    --target aprilgrid.yaml \
    --imu-models calibrated \
    --max-iter 30 \
    --timeoffset-padding 0.03 \
    --export-poses
```

### 多IMU标定

```bash
kalibr_calibrate_imu_camera \
    --bag multi_imu_dataset.bag \
    --cam camchain.yaml \
    --imu imu0.yaml imu1.yaml \
    --imu-models calibrated calibrated \
    --target aprilgrid.yaml \
    --imu-delay-by-correlation
```

### 使用时间段提取

```bash
kalibr_calibrate_cameras \
    --models pinhole-equi \
    --bag large_dataset.bag \
    --topics /cam0/image_raw \
    --target aprilgrid.yaml \
    --bag-from-to 10.0 50.0 \
    --bag-freq 5.0
```

## 9. 关键参数说明

### 标定板参数对标定精度的影响

| 参数 | 推荐值 | 影响 |
|------|--------|------|
| tagSize | 0.088 m | 越大越好，但要在视场内 |
| tagSpacing | 0.3-0.4 | 0.3表示间距为30%，推荐0.3-0.4 |
| tagRows/tagCols | 5-8 | 更多标签提供更多约束 |

### IMU参数获取方式

1. **从IMU数据表**：通常在IMU的技术规格书中给出
2. **从标定结果**：Kalibr会在标定过程中估计这些参数
3. **从其他来源**：已知的参考值（见上面的参考表）

### 相机参数初始值

1. **焦距 (fx, fy)**：
   - 公式：`f = (resolution_x / 2) / tan(fov_x / 2)`
   - 从相机规格书中的视场角推导

2. **主点 (cx, cy)**：
   - 通常为图像中心：`cx = width / 2, cy = height / 2`

3. **畸变系数**：
   - 初始化为0，由标定过程估计

## 10. 输出文件检查清单

标定完成后，检查以下文件：

### 相机标定
- ✅ `*-camchain.yaml` - 参数链
- ✅ `*-results-cam.txt` - 数值结果
- ✅ `*-report-cam.pdf` - 可视化报告（含重投影误差）
- ✅ `*-poses-cam0.csv` - 位姿（如有--export-poses）

### IMU标定
- ✅ `*-camchain-imucam.yaml` - 更新的参数链
- ✅ `*-imu.yaml` - IMU标定参数
- ✅ `*-results-imucam.txt` - 数值结果
- ✅ `*-report-imucam.pdf` - 可视化报告

### 检查事项
- 重投影误差 (报告中的RMS) < 1 像素
- 所有参数收敛（在报告中检查优化迭代）
- IMU-相机时间偏移合理 (通常 < 50 ms)

