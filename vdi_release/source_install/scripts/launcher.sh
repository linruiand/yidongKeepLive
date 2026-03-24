#!/bin/bash

# ============================================
# VDI Dynamic Service Launcher
# ============================================

SERVICE_NAME=$1
ENABLE_STATUS=$2
shift 2
COMMAND="$@"

if [ "$ENABLE_STATUS" = "true" ]; then
    echo ">>> [LAUNCHER] Starting $SERVICE_NAME..."
    # 使用 exec 确保进程接管 PID，方便 Supervisor 管理
    exec $COMMAND
else
    echo ">>> [LAUNCHER] $SERVICE_NAME is DISABLED. Parking process..."
    # 保持进程不退出，防止 Supervisor 反复尝试启动
    exec sleep infinity
fi
