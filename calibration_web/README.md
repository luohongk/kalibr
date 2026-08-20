# Kalibr 自动标定网页

该网页直接调度仓库中的 `auto_calib/run_all.sh`，用于批量运行多相机与 IMU 标定。

## 数据要求

默认扫描 `/home/conanluo/kalibr_data` 的一级子目录。每个可标定目录必须同时包含：

```text
calibration_4cam.bag
calibration_cam0_imu.bag
imu.bag
```

已有 `result/.done` 的目录显示为“已完成”，不会重复提交。需要重跑时，可点击该数据集右侧的“重置标定”，确认后删除整个 `result/`，数据集会立即恢复为“可标定”。重置不会删除三个输入 Bag，正在排队或运行的数据集不能重置。

## 启动

先确认 Kalibr 容器正在运行：

```bash
docker start kalibr_work
docker ps --filter name=kalibr_work
```

然后启动网页：

```bash
cd /root/kalibr
bash calibration_web/start.sh
```

浏览器访问：

```text
http://127.0.0.1:8020
```

局域网访问时使用 `http://<服务器IP>:8020`。前端、API 和实时日志均由同一个 8020 端口提供。

## 可选环境变量

```bash
EGO_WEB_DATA_ROOT=/home/conanluo/kalibr_data
EGO_WEB_PORT=8020
EGO_WEB_HOST=0.0.0.0
```

`run_all.sh` 支持的标定环境变量也会由后端继承，例如 `CAM_BAG_FREQ`、`IMUCAM_BAG_FREQ`、`IMU_SAFETY`、`MODELS`、`KALIBR_EXTRACT_JOBS` 和 `KALIBR_OPT_THREADS`。

任务并发固定为 1，避免多个标定任务争用 Docker、CPU、内存和 `/work`。停止网页服务时，正在排队和运行的任务会标记为 `interrupted`，并终止整个标定进程组。

## 页面功能

- 自动发现满足三个 Bag 要求的数据集
- 批量选择数据集并依次调用 `run_all.sh`
- 展示排队、运行、成功、失败和中断状态
- 实时显示 IMU、Bag 转换、相机、Camera-IMU 和结果发布阶段
- 通过 SSE 实时查看 `runner.log`
- 展示 `summary.txt` 的 OK/WARN/FAIL 质量结论
- 下载 YAML、PDF、TXT 和过程日志
- 二次确认后安全删除已完成数据集的 `result/` 并重新标定
- 在任务中心暂停/继续运行中的标定，或删除等待中及已结束的任务记录；删除记录不会删除原始 Bag 和数据集 `result/`

暂停运行中的任务会同时暂停宿主机任务进程组和专用的 `kalibr_work` 容器；继续标定时会解除容器暂停。请勿在该容器中同时运行与网页无关的其他作业。

健康检查与 API 文档：

```text
http://127.0.0.1:8020/api/v1/health
http://127.0.0.1:8020/docs
```
