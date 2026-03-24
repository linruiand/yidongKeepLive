#!/bin/bash
set -e

# ============================================
# 1. DYNAMIC INSTALLATION
# ============================================
touch /root/.Xauthority
INSTALL_LOG="/var/log/vdi_install.log"
# CMCCHOME="/opt/chuanyun-vdi-client"
CMCCHOME="/opt/apps/com.cmss.saas.ecloudcomputer/files"
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


# ============================================
# 3. SET RUNTIME ENVIRONMENT
# ============================================
APP_DIR="$CMCCHOME"
# export LD_LIBRARY_PATH=$APP_DIR:"$LD_LIBRARY_PATH"
# export LD_LIBRARY_PATH=$APP_DIR/gstreamer-1.0:"$LD_LIBRARY_PATH"
# export LD_LIBRARY_PATH=$APP_DIR/"Device Redirect":"$LD_LIBRARY_PATH"
# export QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage VdSession"
# export QT_PLUGIN_PATH=$APP_DIR/plugins:$QT_PLUGIN_PATH
# export GST_PLUGIN_PATH=$APP_DIR/gstreamer-1.0:$GST_PLUGIN_PATH
# export QT_LOGGING_RULES=
# export QSG_RENDER_LOOP=threaded

# ============================================
# 4. START VDI
# ============================================
# The official binary name based on file inspection
BINARY="$APP_DIR/ecloud-cloud-computer-application"

# --- MANUAL CONFIG (NO STEALTH) ---
# If you don't use the shim library, you MUST pass --no-sandbox explicitly.--disable-dev-shm-usage 
# Note: This will be visible in the process list.
exec "$BINARY" --no-sandbox --remote-debugging-port=9222 "$@"