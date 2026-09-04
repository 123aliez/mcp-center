# CodexMCP：fork 123aliez/codexmcp @ http 分支 + Codex CLI（Node 22）
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI（全局安装，版本钉住便于回溯；升级时改此行并打新镜像 tag）
RUN npm install -g @openai/codex@0.153.0

# uv + codexmcp（钉 SHA：aeae8d9 = http v7（provider 降级 + global 修复））
# venv 装到全局可读路径 /opt/venv（uv 托管的 Python 3.12 也放 /opt，避免 /root 下运行期不可见）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv venv --python 3.12 /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
        "git+https://github.com/123aliez/codexmcp@aeae8d9" \
    && chmod -R a+rX /opt/venv /opt/uv-python
ENV PATH="/opt/venv/bin:${PATH}"

# node 镜像内置 uid 1000（node 用户）：直接复用为运行用户，HOME 固定，
# .codex 凭据/配置持久化由 compose 挂载卷实现
RUN mkdir -p /app/data /workspace /home/node/.codex \
    && chown -R node:node /app/data /workspace /home/node/.codex
ENV HOME=/home/node \
    CODEX_CD_ALLOWLIST=/workspace \
    CODEX_MAX_CONCURRENCY=2

USER node
WORKDIR /workspace
EXPOSE 8322

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD /opt/venv/bin/python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8322/healthz',timeout=3)" || exit 1

CMD ["codexmcp"]
