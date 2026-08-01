# ============================================
# Resume Editor - Cloud Deployment Dockerfile
# WeasyPrint (pure Python PDF) + FastAPI backend
# No browser needed — ~50MB memory vs 300-500MB for Edge/Chromium
# ============================================

FROM python:3.12-slim

# Install system dependencies for WeasyPrint + nginx
# WeasyPrint needs: libpango, libcairo, libgdk-pixbuf (C libraries)
# Fonts: wqy-zenhei for CJK, liberation2 for Latin
RUN apt-get update && apt-get install -y --no-install-recommends \
    # WeasyPrint runtime dependencies
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    # CJK font (~10MB, covers GB2312/GBK)
    fonts-wqy-zenhei \
    # Latin fonts (Arial/Helvetica metric-compatible)
    fonts-liberation2 \
    # nginx for static file serving
    nginx \
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
