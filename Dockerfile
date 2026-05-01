FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS=ignore \
    MAX_WORKERS=8 \
    PORT=9988 \
    DISPLAY=:99

# Xvfb + system libs needed by Chromium. Chromium itself is installed by
# Patchright/Playwright later and lands in ~/.cache/ms-playwright/ — solver.py
# auto-detects it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        dumb-init \
        ca-certificates \
        wget \
        fonts-liberation \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-dejavu-core \
        libnss3 \
        libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libpango-1.0-0 libasound2 libxshmfence1 libglib2.0-0 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt patchright \
    && python -m patchright install chromium \
    && python -m patchright install-deps chromium || true

COPY solver.py service.py entrypoint.sh ./
COPY web ./web
RUN chmod +x entrypoint.sh

EXPOSE 9988

ENTRYPOINT ["/usr/bin/dumb-init", "--", "./entrypoint.sh"]
