## Ubuntu22.04中运行ikalibr

```
docker build -t kalibr:noetic -f Dockerfile_ros1_20_04 .
```


cd 服务器的kalibr目录

运行：

```
不保存编译
docker run -it --rm --name kalibr_work --entrypoint /bin/bash luohongkun0715/kalibr_and_imu_utils:noetic

保存编译
docker run -it --name kalibr_work --entrypoint /bin/bash luohongkun0715/kalibr_and_imu_utils:noetic


```


### 如果要重新编译

```
cd cd /catkin_ws/src
git clone https://github.com/luohongk/imu_utils.git
apt-get install -y libceres-dev ros-noetic-cmake-modules


cd /catkin_ws
rm -rf build devel install
source devel/setup.bash
source /opt/ros/noetic/setup.bash
catkin build -DCMAKE_BUILD_TYPE=Release -j8


```

比如这样：

```
oot@conanluo-LC0:/catkin_ws# rm -rf build devel install
root@conanluo-LC0:/catkin_ws# source /opt/ros/noetic/setup.bash
root@conanluo-LC0:/catkin_ws# catkin build -DCMAKE_BUILD_TYPE=Release
```
