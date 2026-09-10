# syntax=docker/dockerfile:1
FROM node:22.18.0-bookworm-slim AS node

FROM python:3.13.11-slim-bookworm AS assets
WORKDIR /assets
ADD --checksum=sha256:1d0b3b60fd49a344482ef9fbe7d79a433024aaa5542e43ade7e7f211bba2ae47 https://codeload.github.com/novnc/noVNC/zip/refs/tags/v1.6.0 /tmp/novnc.zip
RUN python -m zipfile -e /tmp/novnc.zip /assets && mv /assets/noVNC-1.6.0 /assets/novnc

FROM node AS google-source
RUN apt-get update && apt-get install -y --no-install-recommends unzip ca-certificates && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:aa9f9a8f80bb170d796171586c4c839140b827cf48094b186cc8e38063bd9f6d https://codeload.github.com/iBUHub/AIStudioToAPI/zip/db624c24ab111ad0f50308f20f8a75a216dbf873 /tmp/source.zip
RUN mkdir -p /opt/aistudio && unzip -q /tmp/source.zip -d /tmp/source && mv /tmp/source/* /opt/aistudio/app && rm /tmp/source.zip
WORKDIR /opt/aistudio/app
COPY packaging/aistudio-package-lock.json package-lock.json
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 HUSKY=0
RUN npm ci --ignore-scripts --no-audit --no-fund && node node_modules/vite/bin/vite.js build && npm prune --omit=dev --ignore-scripts

FROM python:3.13.11-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app:app /app/data

FROM base AS web
COPY --from=assets /assets/novnc /app/vendor/novnc
COPY main.py ./
COPY src ./src
COPY configs ./configs
USER app
EXPOSE 8080
CMD ["python", "main.py", "web", "configs/config.docker.yaml"]

FROM base AS google
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates xvfb x11vnc fluxbox fonts-liberation \
    libgtk-3-0 libasound2 libdbus-1-3 libx11-xcb1 libxtst6 libgbm1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=google-source /opt/aistudio /opt/aistudio
# Browser binaries are verified and downloaded at build time, never at first login.
ARG TARGETARCH
COPY docker/download_camoufox.py /tmp/download_camoufox.py
RUN python /tmp/download_camoufox.py "$TARGETARCH" /opt/aistudio/camoufox && \
    mkdir -p /opt/aistudio/node && ln -s /usr/local/bin/node /opt/aistudio/node/node && \
    echo '{"version":"1.3.7","commit":"db624c24ab111ad0f50308f20f8a75a216dbf873"}' > /opt/aistudio/manifest.json
COPY docker/display.sh /usr/local/bin/display.sh
RUN sed -i 's/\r$//' /usr/local/bin/display.sh && chmod +x /usr/local/bin/display.sh
COPY src ./src
ENV JOB_BLESS_AISTUDIO_BUNDLE=/opt/aistudio JOB_BLESS_DATA_DIR=/app/data DISPLAY=:99
USER app
EXPOSE 7860 5900
ENTRYPOINT ["/usr/local/bin/display.sh"]
CMD ["python", "-m", "src.aistudio.service"]
