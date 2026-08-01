# ============================================
# Resume Editor - Cloud Deployment Dockerfile
# Microsoft Edge headless + FastAPI backend
# ============================================

FROM python:3.12-slim

# Install Microsoft Edge + system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Tools needed to add Microsoft's apt repo
    curl \
    gnupg \
    apt-transport-https \
    # Chinese fonts for PDF rendering
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    # Latin fonts (Arial/Helvetica metric-compatible equivalents)
    fonts-liberation2 \
    # nginx for static file serving
    nginx \
    # dbus daemon - Edge/Chromium needs D-Bus in containers,
    # without it Edge hangs waiting for /run/dbus/system_bus_socket
    dbus \
    # curl for healthcheck in docker-entrypoint
    && rm -rf /var/lib/apt/lists/*

# Explicitly tell D-Bus clients (Edge) where the system bus socket is.
# Without this, Edge gets "Could not parse server address: Unknown address type"
# because the container has no desktop session to provide the address.
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
ENV DBUS_SESSION_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket

# Add Microsoft Edge apt repository and install
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-edge.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" > /etc/apt/sources.list.d/microsoft-edge.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends microsoft-edge-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy backend requirements and install
COPY back/requirements.txt /app/back/requirements.txt
RUN pip install --no-cache-dir -r /app/back/requirements.txt

# Copy application files
COPY back/main.py /app/back/main.py
COPY index.html /app/index.html
COPY resume-editor.html /app/resume-editor.html
COPY about.html /app/about.html
COPY styles.css /app/styles.css
COPY sw.js /app/sw.js
COPY logo.ico /app/logo.ico
COPY logo.png /app/logo.png
COPY wechat_public.bmp /app/wechat_public.bmp
COPY wechat_qr.png /app/wechat_qr.png

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Remove default nginx site
RUN rm -f /etc/nginx/sites-enabled/default

# Copy startup script
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 80

CMD ["/app/docker-entrypoint.sh"]
