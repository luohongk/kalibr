  填好后运行（两条命令，容器内跑）

  docker run -it --rm --name kalibr_work --entrypoint /bin/bash \
  -v /root/kalibr:/catkin_ws/src/kalibr \
  -v /root/imu_utils:/catkin_ws/src/imu_utils \
  -v /home/conanluo/kalibr_data:/data \
  luohongkun0715/kalibr_and_imu_utils:noetic-fixed
  
  
  # 1) 同步脚本 + camchain + 标定板 进容器
  docker cp /root/kalibr/auto_calib/calib_cam0_imu kalibr_work:/opt/auto_calib/
  docker cp /root/kalibr/calibration_board_data/. kalibr_work:/opt/auto_calib/calib_cam0_imu/

  # 2) 容器内执行 (宿主机 /home/conanluo/kalibr_data/EGO2 → 容器 /data/EGO2)
 docker exec -it -e TARGET_NAME=checkerboard.yaml kalibr_work bash
bash /opt/auto_calib/run_cam0_imu.sh /data/EGO2

# 把结果拷贝到/root/kalibr/auto_calib/calib_cam0_imu
  mkdir -p /root/kalibr/auto_calib/calib_cam0_imu/result_cam0_imu/EGO2
  cp -r /home/conanluo/kalibr_data/EGO2/result_cam0_imu/. \
        /root/kalibr/auto_calib/calib_cam0_imu/result_cam0_imu/EGO2/