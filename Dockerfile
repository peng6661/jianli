# ============================================
# Resume Editor - Cloud Deployment Dockerfile
# Multi-stage: Playwright + FastAPI backend
# ============================================

FROM python:3.12-slim

# Install system dependencies for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium runtime dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    # Chinese fonts for PDF rendering
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    # nginx for static file serving
    nginx \
    # cleanup
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy backend requirements and install
COPY back/requirements.txt /app/back/requirements.txt
RUN pip install --no-cache-dir -r /app/back/requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium

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
