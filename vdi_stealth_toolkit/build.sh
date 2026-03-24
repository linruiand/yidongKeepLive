#!/bin/bash
# VDI Stealth SO Builder
# This script uses a docker image to compile the SO for Ubuntu 20.04 compatibility.

set -e

TOOLKIT_DIR=$(cd "$(dirname "$0")" && pwd)
SRC_DIR="$TOOLKIT_DIR/src"
BIN_DIR="$TOOLKIT_DIR/bin"
IMAGE_NAME="vdi_stealth_builder"
OUTPUT_SO="libudev-shim.so"

mkdir -p "$BIN_DIR"

echo ">>> Phase 1: Building Build Environment..."
docker build -t "$IMAGE_NAME" -f "$TOOLKIT_DIR/Toolkit.Dockerfile" "$TOOLKIT_DIR"

echo ">>> Phase 2: Compiling Stealth SO..."
docker run --rm \
    -v "$SRC_DIR":/src \
    -v "$BIN_DIR":/output \
    "$IMAGE_NAME" \
    bash -c "gcc -fPIC -shared -fno-stack-protector -s -o /output/$OUTPUT_SO /src/stealth_injector.c -ldl"

echo ">>> Build Success: $BIN_DIR/$OUTPUT_SO"
echo ">>> Phase 3: Syncing to VDI release project..."

DEST_DIR="/home/r/yun/vdi_release/source_install/libs"
mkdir -p "$DEST_DIR"
cp "$BIN_DIR/$OUTPUT_SO" "$DEST_DIR/"

echo ">>> [DONE] Library placed at $DEST_DIR/$OUTPUT_SO"
echo ">>> Your main VDI Dockerfile can now simply COPY this file."
