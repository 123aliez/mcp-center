# amap-maps（sugarforever/amap-mcp-server，PyPI 钉版本构建可复现）
# 选型：官方 npm 版 stdio-only 需桥接层；此 Python 版 CLI 原生支持 streamable-http（16 工具，含地址版+坐标版路线）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# uv 装到 venv（codexmcp 同款方案：--system 在 slim 镜像无系统 Python）
# 审查修复#6：钉住验证过的关键依赖（zotero 同理）——mcp 1.x 行为差异大（settings 字段/Host 防护），升级须重验
RUN uv venv /opt/venv --python 3.12 \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
        "amap-mcp-server==0.1.11" "mcp==1.8.1" "httpx>=0.27,<1" \
    && chmod -R a+rX /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/appuser

COPY wrapper.py /app/wrapper.py

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R 1000:1000 /app/data
USER appuser

EXPOSE 8323
# 审查修复#2：TCP 探测替代 MCP initialize 握手——1.8.1 有状态 session manager 无闲置回收，
# 每分钟一次裸 initialize 会无限积累会话（~1440/天）
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import socket;s=socket.create_connection(('127.0.0.1',8323),timeout=3);s.close()" || exit 1

CMD ["python", "/app/wrapper.py"]
