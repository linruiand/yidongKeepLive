#!/bin/bash
set -e

# Identification (Hostname-driven)
T=$(hostname)
# --- CONFIGURATION REGISTRY ---
case "$T" in
    jty*)      S="entrypoint-vdi_jty.sh" ;;
    suzou*)    S="entrypoint-vdi_suzou.sh" ;;
    hangzhou*) S="entrypoint-vdi_hangzhou.sh" ;;
    tyy*)      S="entrypoint-vdi_tyy.sh" ;;
    *)         echo "Unknown VDI_TYPE: $T"; exit 1 ;;
esac


# Wait for X Server (Common)
echo ">>> Waiting for X server (:99)..."
for i in {1..30}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then
        echo ">>> X server is ready"
        break
    fi
    sleep 1
done

# --- EXECUTION (PARAM FORWARDING) ---
echo ">>> Routing to specific implementation: $S"
exec "/app/scripts/$S" "$@"