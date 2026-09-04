# zotero-mcp（kujenga/zotero-mcp，PyPI 钉版本构建可复现）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 审查修复#6：钉住验证过的 mcp 2.x 小版本（wrapper 依赖 2.x 的 run() kwargs 与 TransportSecuritySettings 行为）
RUN uv venv /opt/venv --python 3.12 \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
        "zotero-mcp==0.3.1" "mcp==2.1.1" \
    && chmod -R a+rX /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/appuser

COPY wrapper.py /app/wrapper.py

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R 1000:1000 /app/data
USER appuser

EXPOSE 8324
# 审查修复#2：TCP 探测替代 MCP initialize 握手（避免周期性会话积累，同 amap）
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket;s=socket.create_connection(('127.0.0.1',8324),timeout=3);s.close()" || exit 1

CMD ["python", "/app/wrapper.py"]
