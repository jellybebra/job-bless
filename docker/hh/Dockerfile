# The same image is used by Compose and the native application on Windows/Linux.
FROM python:3.13.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc fluxbox ca-certificates fonts-liberation \
    libgtk-3-0 libasound2 libdbus-1-3 libx11-xcb1 libxtst6 libgbm1 \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 1000 app \
    && mkdir -p /data/profile && chown -R app:app /data
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra browser-server
ARG TARGETARCH
COPY docker/download_camoufox.py /tmp/download_camoufox.py
RUN python /tmp/download_camoufox.py "$TARGETARCH" /opt/camoufox hh
ADD --checksum=sha256:1d0b3b60fd49a344482ef9fbe7d79a433024aaa5542e43ade7e7f211bba2ae47 https://codeload.github.com/novnc/noVNC/zip/refs/tags/v1.6.0 /tmp/novnc.zip
RUN python -m zipfile -e /tmp/novnc.zip /opt && mv /opt/noVNC-1.6.0 /opt/novnc && rm /tmp/novnc.zip
COPY docker/display.sh /usr/local/bin/display.sh
COPY docker/hh/server.py docker/hh/server.cjs /app/
RUN sed -i 's/\r$//' /usr/local/bin/display.sh && chmod +x /usr/local/bin/display.sh
USER app
ENV DISPLAY=:99
EXPOSE 3000 5900
HEALTHCHECK --interval=5s --timeout=3s --start-period=30s --retries=6 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1',3000),2).close()"
ENTRYPOINT ["/usr/local/bin/display.sh"]
CMD ["python", "/app/server.py"]
