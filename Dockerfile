# syntax=docker/dockerfile:1
FROM python:3.11-slim

# 環境變數
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=5000

# 安裝系統相依（Playwright --with-deps 會再補齊）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    ca-certificates \
    dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

# 安裝 Chromium 與其相依套件
RUN python -m playwright install --with-deps chromium

# 置入程式
COPY . /app

# 使用較安全的啟動器
ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["python", "app.py"]
EXPOSE 5000