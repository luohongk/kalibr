
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
