#!/bin/bash
set -e

echo "========================================"
echo "  Resume Editor - Cloud Deployment"
echo "  PDF Engine: Edge (Blink = same as browser)"
echo "========================================"
echo ""

# ============================================
# 1. Create swap file (2GB) if no swap exists
#    Server has limited RAM (~435MB available, 0 swap).
#    Edge/Chromium needs 300-500MB. Swap provides virtual memory.
# ============================================
echo "[1/4] Checking swap..."
if swapon --show 2>/dev/null | grep -q swap; then
    echo "  Swap already active, skipping."
else
    echo "  Creating 2GB swap file..."
    dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "  Swap enabled successfully."
    swapon --show
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
