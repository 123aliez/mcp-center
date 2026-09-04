# GrokSearch：fork 123aliez/GrokSearch @ http-admin 分支，钉 commit SHA 构建可复现
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# GitHub 不可达环境：docker build --build-arg HTTPS_PROXY=...
ARG HTTPS_PROXY=""
ENV HTTPS_PROXY=${HTTPS_PROXY}

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 钉 SHA 安装（8b283b5 = http-admin v2（config.json 双源+热重载），升级时改此 SHA 并打新 tag）
RUN uv pip install --system --no-cache \
    "git+https://github.com/123aliez/GrokSearch@8b283b5"

ENV GROK_CONFIG_DIR=/app/data \
    GROK_LOG_DIR=/app/logs \
    HOME=/home/appuser

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R 1000:1000 /app/data /app/logs
USER appuser

EXPOSE 8321
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8321/healthz',timeout=3)" || exit 1

CMD ["grok-search"]
