#!/bin/bash
set -e

echo "========================================"
echo "  Resume Editor - Cloud Deployment"
echo "  PDF Engine: Microsoft Edge headless"
echo "========================================"
echo ""

# Start D-Bus system daemon (Edge/Chromium requires it)
# Without dbus-daemon, Edge hangs indefinitely on D-Bus connect attempts
mkdir -p /run/dbus
rm -f /run/dbus/system_bus_socket  # Remove stale socket from previous run
if command -v dbus-daemon &>/dev/null; then
    dbus-daemon --system --fork 2>&1 || echo "  WARNING: dbus-daemon failed to start"
    # Wait for socket to appear
    for i in $(seq 1 5); do
        if [ -S /run/dbus/system_bus_socket ]; then
            echo "  D-Bus system bus ready at /run/dbus/system_bus_socket"
            break
        fi
        sleep 0.5
    done
    # Verify
    if [ ! -S /run/dbus/system_bus_socket ]; then
        echo "  WARNING: D-Bus socket not created! Edge may hang."
    fi
else
    echo "  WARNING: dbus-daemon not found, Edge may hang on D-Bus"
fi

# Start FastAPI backend in background
echo "[1/2] Starting FastAPI PDF service (port 8080)..."
cd /app
uvicorn back.main:app --host 127.0.0.1 --port 8080 --log-level info &
BACKEND_PID=$!

# Wait for backend to be ready
echo "  Waiting for backend to be ready..."
for i in $(seq 1 15); do
    if curl -s http://127.0.0.1:8080/api/health > /dev/null 2>&1; then
        echo "  Backend is ready."
        break
    fi
    sleep 1
    if [ $i -eq 15 ]; then
        echo "  WARNING: Backend not responding after 15s, starting nginx anyway."
    fi
done

# Start Nginx in foreground
echo "[2/2] Starting Nginx (port 80)..."
echo ""
echo "  Site:     http://localhost"
echo "  API:      http://localhost/api/health"
echo "  Export:   POST http://localhost/api/export-pdf"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Graceful shutdown on signal
trap "echo ''; echo 'Shutting down...'; kill $BACKEND_PID 2>/dev/null; nginx -s quit 2>/dev/null; exit 0" SIGTERM SIGINT

nginx -g 'daemon off;'
