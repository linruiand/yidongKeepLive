#!/bin/bash
# VDI 自动化启动脚本

export DISPLAY=:99

echo ">>> 等待 X 服务器就绪..."
for i in {1..30}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo ">>> X 服务器就绪"
        break
    fi
    sleep 1
done

echo ">>> 等待 VDI 应用启动..."
sleep 10

echo ">>> 启动自动化脚本 (VDI_TYPE: $(hostname))..."
cd "$(dirname "$0")"

T=$(hostname)
case "$T" in
    jty*)      PY="vdi_automation_jty.py" ;;
    suzou*)    PY="vdi_automation_suzou.py" ;;
    hangzhou*) PY="vdi_automation_hangzhou.py" ;;
    tyy*)      PY="vdi_automation_tyy.py" ;;
    *)         PY="vdi_automation_jty.py" ;;
esac

# Fallback to default if specific one doesn't exist
if [ ! -f "$PY" ]; then
    PY="vdi_automation_jty.py"
fi

exec python3 "$PY"
