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
    fontconfig \
    dbus \
    nginx \
    curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create custom fonts directory and copy built-in fonts
RUN mkdir -p /usr/share/fonts/custom
COPY fonts/ /usr/share/fonts/custom/

# Fontconfig:
# 1) Microsoft YaHei / 微软雅黑: built-in msyh.ttc takes priority.
#    This is the PRIMARY font for resume content (sans-serif / heiti design).
# 2) Generic sans-serif: prepend Microsoft YaHei for CJK heiti rendering.
#    Falls back to WenQuanYi Zen Hei if YaHei is unavailable.
# 3) Generic serif: falls back to Microsoft YaHei / WenQuanYi for CJK.
# 4) Arial / Helvetica Neue → Liberation Sans (matches browser behaviour).
RUN cat > /etc/fonts/local.conf << 'FONTCONF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!-- Custom fonts dir (built-in fonts + optional volume mounts) -->
  <dir>/usr/share/fonts/custom</dir>

  <!-- ===== Microsoft YaHei / 微软雅黑: prepend so built-in msyh.ttc wins ===== -->
  <match target="pattern">
    <test name="family"><string>Microsoft YaHei</string></test>
    <edit name="family" mode="prepend" binding="strong"><string>Microsoft YaHei</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>微软雅黑</string></test>
    <edit name="family" mode="prepend_first" binding="strong"><string>Microsoft YaHei</string></edit>
  </match>

  <!-- ===== Generic sans-serif: prepend Microsoft YaHei for CJK heiti-style rendering ===== -->
  <match target="pattern">
    <test name="family"><string>sans-serif</string></test>
    <edit name="family" mode="prepend" binding="weak"><string>Microsoft YaHei</string></edit>
    <edit name="family" mode="append" binding="weak"><string>WenQuanYi Zen Hei</string></edit>
  </match>

  <!-- ===== Generic serif: fall back to heiti fonts for CJK ===== -->
  <match target="pattern">
    <test name="family"><string>serif</string></test>
    <edit name="family" mode="append" binding="weak"><string>Microsoft YaHei</string></edit>
    <edit name="family" mode="append" binding="weak"><string>WenQuanYi Zen Hei</string></edit>
  </match>

  <!-- ===== Arial / Helvetica Neue: explicit mapping to Liberation Sans ===== -->
  <match target="pattern">
    <test name="family"><string>Arial</string></test>
    <edit name="family" mode="assign" binding="strong"><string>Liberation Sans</string></edit>
  </match>
  <match target="pattern">
    <test name="family"><string>Helvetica Neue</string></test>
    <edit name="family" mode="assign" binding="strong"><string>Liberation Sans</string></edit>
  </match>
</fontconfig>
FONTCONF
RUN fc-cache -f

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
