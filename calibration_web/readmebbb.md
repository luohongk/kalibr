/root/kalibr/calibration_web 已完成 Kalibr 流程收尾，现在会直接调用：

  bash auto_calib/run_all.sh <dataset>

  已完成：

  - 扫描 /home/conanluo/kalibr_data 中包含三个必需 Bag 的目录
  - 批量提交标定任务，固定单并发
  - 展示真实标定阶段和实时日志
  - 展示 summary.txt 的 OK/WARN/FAIL 结论
  - 下载 YAML、PDF、TXT 和日志
  - 前后端统一由 8000 端口提供
  - 后端测试：110 passed
  - 前端测试：10 passed
  - TypeScript 与生产构建通过
  - 真实目录冒烟验证通过：发现 14 个数据集，其中 13 个已完成、1 个可标定

  启动方式：

  docker run -it --rm --name kalibr_work --entrypoint /bin/bash \
  -v /root/kalibr:/catkin_ws/src/kalibr \
  -v /root/imu_utils:/catkin_ws/src/imu_utils \
  -v /home/conanluo/kalibr_data:/data \
  luohongkun0715/kalibr_and_imu_utils:noetic-fixed

  docker start kalibr_work

  cd /root/kalibr
  bash calibration_web/start.sh

  浏览器打开：

  http://127.0.0.1:8020

  完整说明见 calibration_web/README.md，启动脚本是 calibration_web/start.sh。

  注意：当前 docker ps 没有运行中的容器，所以提交标定前必须先启动 kalibr_work。这次没有触发耗时的真实标定任务，只验证了网页、API、真实数据发现和路由。另一个需要注意的点是整个
  calibration_web/ 目前仍是 Git 未跟踪目录，尚未提交。