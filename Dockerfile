FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS=ignore \
    MAX_WORKERS=8 \
    PORT=9988 \
    CAMOUFOX_HEADLESS=virtual

# System deps for Firefox + Camoufox's internal virtual display (Xvfb).
# Camoufox bundles its own Firefox binary via `python -m camoufox fetch`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        dumb-init \
        ca-certificates \
        wget \
        curl \
        fonts-liberation \
        fonts-noto-core \
        fonts-noto-cjk \
        fonts-dejavu-core \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libxt6 \
        libasound2 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libpango-1.0-0 \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libxkbcommon0 \
        libgbm1 \
        libglib2.0-0 \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m camoufox fetch

COPY solver.py service.py entrypoint.sh ./
COPY web ./web
RUN chmod +x entrypoint.sh

EXPOSE 9988

ENTRYPOINT ["/usr/bin/dumb-init", "--", "./entrypoint.sh"]
