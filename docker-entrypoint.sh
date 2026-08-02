#!/bin/bash
set -e

echo "========================================"
echo "  Resume Editor - Cloud Deployment"
echo "  PDF Engine: Edge (Blink = same as browser)"
echo "========================================"
echo ""

# ============================================
# 1. Create swap file (1GB) if no swap exists
#    1GB RAM + 1GB swap = 2GB virtual memory.
#    4 concurrent Edge processes (~600MB) fit comfortably.
#
#    IMPORTANT: swap file MUST be on ext4/xfs, NOT overlayfs.
#    Docker container root fs is overlayfs → swapon returns EINVAL.
#    We use a Docker named volume (/swap) which is backed by host ext4.
# ============================================
SWAP_FILE=/swap/swapfile
echo "[1/4] Checking swap..."
if swapon --show 2>/dev/null | grep -q swap; then
    echo "  Swap already active, skipping."
else
    echo "  Creating 1GB swap file on Docker volume (/swap)..."
    # fallocate is instant (pre-allocates space without writing zeros)
    if fallocate -l 1G "$SWAP_FILE" 2>/dev/null; then
        echo "  Allocated via fallocate (instant)."
    else
        echo "  fallocate not supported, falling back to dd (slow)..."
        dd if=/dev/zero of="$SWAP_FILE" bs=1M count=1024 status=progress
    fi
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
    # swapon may fail (e.g. unsupported fs) — don't let set -e kill the script
    swapon "$SWAP_FILE" 2>&1 || true
    if swapon --show 2>/dev/null | grep -q swap; then
        echo "  Swap enabled successfully."
        swapon --show
    else
        echo "  WARNING: swapon failed! Edge may OOM on large PDFs."
        echo "  Continuing without swap..."
    fi
fi

# ============================================
# 2. Start D-Bus daemon (Edge needs it on Linux)
# ============================================
echo "[2/4] Starting D-Bus daemon..."
mkdir -p /run/dbus
rm -f /run/dbus/system_bus_socket  # Clean stale socket
dbus-daemon --system --fork
echo "  D-Bus started (pid: $(cat /run/dbus/system_bus_socket.pid 2>/dev/null || echo '?'))."

# ============================================
# 3. Start FastAPI backend
# ============================================
echo "[3/4] Starting FastAPI PDF service (port 8080)..."
cd /app
uvicorn back.main:app --host 127.0.0.1 --port 8080 --log-level info &
BACKEND_PID=$!

# Wait for backend to be ready
echo "  Waiting for backend to be ready..."
for i in $(seq 1 20); do
    if curl -s http://127.0.0.1:8080/api/health > /dev/null 2>&1; then
        echo "  Backend is ready."
        break
    fi
    sleep 1
    if [ $i -eq 20 ]; then
        echo "  WARNING: Backend not responding after 20s, starting nginx anyway."
    fi
done

# ============================================
# 4. Start Nginx
# ============================================
echo "[4/4] Starting Nginx (port 80)..."
echo ""
echo "  Site:     http://localhost"
echo "  API:      http://localhost/api/health"
echo "  Export:   POST http://localhost/api/export-pdf"
echo "  Test:     GET http://localhost/api/test-pdf"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Graceful shutdown on signal
trap "echo ''; echo 'Shutting down...'; kill $BACKEND_PID 2>/dev/null; nginx -s quit 2>/dev/null; exit 0" SIGTERM SIGINT

nginx -g 'daemon off;'
