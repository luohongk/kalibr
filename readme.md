## Ubuntu22.04中运行ikalibr

```
docker build -t kalibr:noetic -f Dockerfile_ros1_20_04 .
```

运行：

```
xhost +local:root && \
docker run -it \
--network=host \
--privileged \
-v /tmp/.X11-unix:/tmp/.X11-unix \
-e DISPLAY=$DISPLAY \
-v /home/lhk/workspace/kalibr:/catkin_ws/src/kalibr \
-v /home/lhk/data:/data \
--name kalibr_work \
-w / \
kalibr:noetic

```


### 如果要重新编译

```
cd /catkin_ws
rm -rf build devel install
source devel/setup.bash
source /opt/ros/noetic/setup.bash
catkin build -DCMAKE_BUILD_TYPE=Release
```

比如这样：

```
oot@conanluo-LC0:/catkin_ws# rm -rf build devel install
root@conanluo-LC0:/catkin_ws# source /opt/ros/noetic/setup.bash
root@conanluo-LC0:/catkin_ws# catkin build -DCMAKE_BUILD_TYPE=Release
```

### 多目标定
 rosrun kalibr kalibr_calibrate_imu_camera --bag /data/Calibration/scripts/kalibr_input.bag ..

好了。现在 Docker 里执行相机标定：
                                                                                          
  source /catkin_ws/devel/setup.bash
                                                                                                                                                                                                                    
rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/kalibr_input.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models pinhole-equi1 pinhole-equi pinhole-equi pinhole-equi --target /data/Calibration/scripts/aprilgrid.yaml --bag-freq 4 --dont-show-report


rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/kalibr_input.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models omni-radtan omni-radtan omni-radtan omni-radtan --target /data/Calibration/scripts/aprilgrid.yaml --bag-freq 4 --dont-show-report

rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/kalibr_input.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models pinhole-equi pinhole-equi pinhole-equi pinhole-equi --target /data/Calibration/scripts/aprilgrid.yaml --bag-freq 4 --dont-show-report

rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/kalibr_input.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models omni-radtan omni-radtan omni-radtan omni-radtan --target /data/Calibration/scripts/checkerboard.yaml --bag-freq 4 --dont-show-report

rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/kalibr_input_filtered.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models omni-radtan omni-radtan omni-radtan omni-radtan --target /data/Calibration/scripts/checkerboard.yaml --bag-freq 2 --dont-show-report



rosrun kalibr kalibr_calibrate_cameras --bag /data/Calibration/scripts/四目鱼眼相机标定整理/input_data/kalibr_input.bag --topics /cam0/image_raw /cam1/image_raw /cam2/image_raw /cam3/image_raw --models ds-none ds-none ds-none ds-none --target /data/Calibration/scripts/checkerboard.yaml --bag-freq 2 --dont-show-report


rosrun kalibr kalibr_calibrate_imu_camera \
      --bag /data/Calibration/scripts/四目鱼眼相机标定整理/input_data/kalibr_input_imu.bag \
      --cam /data/Calibration/scripts/四目鱼眼相机标定整理/input_data/kalibr_input-camchain.yaml \
      --imu /data/Calibration/scripts/四目鱼眼相机标定整理/imu.yaml \
      --target /data/Calibration/scripts/四目鱼眼相机标定整理/checkerboard.yaml
