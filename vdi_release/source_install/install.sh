#!/bin/bash

# ============================================
# VDI Virtual Desktop Installer (Multi-Instance)
# ============================================

INSTANCE_ID=${1:-0}
VNC_PORT_BASE=${2:-5900}
NOVNC_PORT_BASE=${3:-6080}

# Calculate offsets
CONTAINER_NAME="uos_instance_$INSTANCE_ID"
VNC_PORT=$((VNC_PORT_BASE + INSTANCE_ID))
NOVNC_PORT=$((NOVNC_PORT_BASE + INSTANCE_ID))

# Path definitions
# 1. 第一步：先跳进脚本所在的目录，确保后续相对路径（如 docker/Dockerfile）正确
cd "$(dirname "$0")" || exit 1
# 2. 第二步：锁定绝对路径，用于 Docker -v 挂载
BASE_DIR=$(pwd)

SHARED_DEB="$BASE_DIR/config/vdi_client.deb"
PRIVATE_CONFIG_DIR="$BASE_DIR/config/config_$INSTANCE_ID"
PRIVATE_LOG_DIR="$BASE_DIR/logs/logs_$INSTANCE_ID"

echo "========================================"
echo "   VDI Instance: $INSTANCE_ID"
echo "   VNC Port:     $VNC_PORT"
echo "   noVNC Port:   $NOVNC_PORT"
echo "   Container:    $CONTAINER_NAME"
echo "========================================"

# 1. Initialize private config from templates
if [ ! -d "$PRIVATE_CONFIG_DIR" ]; then
    echo ">>> Initializing private config for instance $INSTANCE_ID..."
    mkdir -p "$PRIVATE_CONFIG_DIR"
    # Copy templates if they exist
    [ -f "$BASE_DIR/config/credentials.conf" ] && cp "$BASE_DIR/config/credentials.conf" "$PRIVATE_CONFIG_DIR/"
    [ -f "$BASE_DIR/config/vnc_password" ] && cp "$BASE_DIR/config/vnc_password" "$PRIVATE_CONFIG_DIR/"
fi
mkdir -p "$PRIVATE_LOG_DIR"

# 1.5 解析服务开关配置 (从私有配置读取，如果没有则默认开启)
E_VNC=$(grep "enable_vnc=" "$PRIVATE_CONFIG_DIR/credentials.conf" | cut -d'=' -f2 | xargs)
E_NOVNC=$(grep "enable_novnc=" "$PRIVATE_CONFIG_DIR/credentials.conf" | cut -d'=' -f2 | xargs)
E_VNC=${E_VNC:-true}
E_NOVNC=${E_NOVNC:-true}

# V_TYPE=$(grep "vdi_type=" "$PRIVATE_CONFIG_DIR/credentials.conf" | cut -d'=' -f2 | xargs)
V_TYPE=${V_TYPE:-jty}
# V_TYPE=${V_TYPE:-suzou}

# 2. Prepare Image
if [ -f "uos_vdi_image.tar" ]; then
    echo ">>> Loading image from tarball..."
    docker load -i uos_vdi_image.tar
elif docker image inspect uos-gui:latest >/dev/null 2>&1; then
    echo ">>> Image uos-gui:latest already exists locally."
elif [ -f "docker/Dockerfile" ]; then
    echo ">>> Building image from docker/Dockerfile..."
    docker build -t uos-gui:latest -f docker/Dockerfile .
else
    echo ">>> Error: No uos_vdi_image.tar, local image, or Dockerfile found!"
    exit 1
fi


# 3. Cleanup Old Container
echo ">>> Stopping old instance: $CONTAINER_NAME..."
docker rm -f $CONTAINER_NAME 2>/dev/null || true

# 4. Run Container with Dual-Mount Strategy
echo ">>> Starting instance $INSTANCE_ID..."
docker run -d \
    --name $CONTAINER_NAME \
    -h "$V_TYPE" \
    --restart always \
    --cap-add=NET_ADMIN \
    --cap-add=SYS_ADMIN \
    --security-opt seccomp=unconfined \
    --shm-size=1.5g \
    --memory=2G \
    -p $NOVNC_PORT:6080 \
    -p $VNC_PORT:5900 \
    \
    -v /dev/dri:/dev/dri \
    -v /run/dbus:/run/dbus:ro \
    \
    -v /sys/class/dmi:/sys/class/dmi:ro \
    -v /sys/firmware:/sys/firmware:ro \
    -v /sys/devices/virtual/dmi:/sys/devices/virtual/dmi:ro \
    \
    -v /proc/cpuinfo:/proc/cpuinfo:ro \
    \
    -e LANG=zh_CN.UTF-8 \
    -e TZ=Asia/Shanghai \
    -e ENABLE_VNC=$E_VNC \
    -e ENABLE_NOVNC=$E_NOVNC \
    \
    -v "$PRIVATE_CONFIG_DIR":/config \
    -v "$PRIVATE_LOG_DIR":/var/log/supervisor \
    -v "$SHARED_DEB":/pkg/vdi_client.deb:ro \
    \
    uos-gui:latest

if [ $? -eq 0 ]; then
    echo ""
    echo "========================================"
    echo "   ✅ VDI Instance $INSTANCE_ID 启动成功!"
    echo "========================================"
    echo ""
    echo "访问地址: http://localhost:$NOVNC_PORT/vnc.html"
    echo "配置文件: $PRIVATE_CONFIG_DIR/credentials.conf"
    echo "查看日志: docker logs -f $CONTAINER_NAME"
    echo ""
else
    echo ">>> Error: Failed to start container!"
    exit 1
fi
