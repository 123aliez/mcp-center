# mcp-admin：统一管理/鉴权服务（单 worker 保证 tokens.json 单写者）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY app.py .

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data /app/secrets \
    && chown -R 1000:1000 /app/data
ENV HOME=/home/appuser
USER appuser

EXPOSE 8330
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8330/healthz',timeout=3)" || exit 1
# 单 worker（--workers 1）：TokenStore 内存缓存与文件 flush 的单写者前提
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8330", "--workers", "1"]
