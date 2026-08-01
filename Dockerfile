# ============================================
# Resume Editor - Cloud Deployment Dockerfile
# PDF Engine: Edge (Blink = same as browser preview)
# Swap file created at runtime to handle Edge memory needs
# ============================================

FROM python:3.12-slim

# ============================================
# Layer 1: Base packages (fonts, dbus, nginx, curl, gnupg)
# This layer is cached separately from Edge installation.
# ============================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei \
    fonts-liberation2 \
    dbus \
    nginx \
    curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================
# Layer 2: Install Microsoft Edge
# Download GPG key with retry (server TLS can be flaky).
# Falls back to wget if curl fails.
# ============================================
RUN for i in 1 2 3 4 5; do \
        echo "Attempt $i: downloading Microsoft GPG key..." && \
        curl -fsSL --connect-timeout 15 --max-time 60 \
            https://packages.microsoft.com/keys/microsoft.asc \
            -o /tmp/microsoft.asc && break || \
        (echo "curl failed, trying wget..." && \
         wget -q --timeout=60 -O /tmp/microsoft.asc \
            https://packages.microsoft.com/keys/microsoft.asc && break) || \
        (echo "Attempt $i failed, retrying in 5s..." && sleep 5); \
    done && \
    gpg --dearmor -o /usr/share/keyrings/microsoft-edge.gpg /tmp/microsoft.asc && \
    rm -f /tmp/microsoft.asc && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" \
        > /etc/apt/sources.list.d/microsoft-edge.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends microsoft-edge-stable && \
    rm -rf /var/lib/apt/lists/*

# D-Bus environment (Edge connects to D-Bus on Linux)
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
ENV DBUS_SESSION_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket

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
