#!/bin/bash
set -e

# ============================================
# 1. DYNAMIC INSTALLATION
# ============================================
touch /root/.Xauthority
INSTALL_LOG="/var/log/vdi_install.log"
CMCCHOME="/opt/chuanyun-vdi-client"
DEB_SOURCE="/pkg/vdi_client.deb"

if [ ! -d "$CMCCHOME" ]; then
    echo ">>> [INSTALL] VDI Client not found at $CMCCHOME" | tee -a "$INSTALL_LOG"
    if [ -f "$DEB_SOURCE" ]; then
        echo ">>> [INSTALL] Found $DEB_SOURCE, installing at $(date)..." | tee -a "$INSTALL_LOG"
        # Track installation detailed log
        if ! dpkg -i "$DEB_SOURCE" >> "$INSTALL_LOG" 2>&1; then
            echo ">>> [INSTALL] dpkg issues detected, running dependency fix..." | tee -a "$INSTALL_LOG"
            apt-get install -f -y >> "$INSTALL_LOG" 2>&1
        fi
        echo ">>> [INSTALL] Installation complete. Logs at $INSTALL_LOG" | tee -a "$INSTALL_LOG"
    else
        echo ">>> [ERROR] VDI Client missing and no package at $DEB_SOURCE" | tee -a "$INSTALL_LOG"
        echo ">>> Mirror the host 'config' directory to container '/config' and put 'vdi_client.deb' there."
        exit 1
    fi
fi

# ============================================
# 2. X SERVER SYNC
# ============================================
echo ">>> Waiting for X server (:99)..."
for i in {1..30}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo ">>> X server is ready"
        break
    fi
    sleep 1
done

# Define paths for services
# APP_DIR="${CMCCHOME}/resources/app.asar.unpacked/node_modules"
# ZTE_INS_DIR="${APP_DIR}/chuanyunAddOn-zte/ccsdk"

# Start zqoe service if exists via supervisor
# if [ -d "$ZTE_INS_DIR" ]; then
#     echo ">>> Telling supervisor to start qoe service..."
#     supervisorctl start qoe || echo ">>> [WARNING] Could not start qoe via supervisor"
# fi

# ============================================
# 3. PATCH OFFICIAL LAUNCHER
# ============================================
OFFICIAL_LAUNCHER="${CMCCHOME}/launch-app.sh"
# if [ -f "$OFFICIAL_LAUNCHER" ]; then
#     if ! grep -q "no-sandbox" "$OFFICIAL_LAUNCHER"; then
#         echo ">>> [PATCH] Patching official launcher to inject flags..."
#         # Replace %U with our security/debug flags and allow argument forwarding
#         sed -i 's/%U/--no-sandbox --remote-debugging-port=9222 "$@"/g' "$OFFICIAL_LAUNCHER"
#     fi
# fi

# ============================================
# 4. START VDI (STEALTH MODE - ACTIVE)
# ============================================
# Note: We now use LD_PRELOAD to inject flags directly into memory.
# This keeps 'ps -ef' clean and the source files original.
echo ">>> Starting VDI via official launcher (Stealth Mode Activated)..."
# 动态库主要是添加  --remote-debugging-port=9222 功能
export LD_PRELOAD=/usr/local/lib/libudev-shim.so
exec "$OFFICIAL_LAUNCHER"