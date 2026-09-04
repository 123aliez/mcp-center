# amap wrapper：读 data/config.json 注入 env（密钥只经管理页，不进 compose/.env）
# → import amap_mcp_server（模块级读 env，必须先注入）→ settings 覆盖法起 streamable-http
# amap-mcp-server 钉 0.1.11（依赖 mcp 1.8.1：无 TransportSecuritySettings，无 DNS rebinding 防护，Host 校验无需处理）
import json
import os
import sys
from pathlib import Path

# 审查修复#3：白名单键——config.json 失陷时不能向进程注入任意 env（HTTP_PROXY/MCP_HTTP_PORT 等）
_ALLOWED_KEYS = {"AMAP_MAPS_API_KEY"}

cfg_path = Path(os.environ.get("AMAP_CONFIG_FILE", "/app/data/config.json"))
if cfg_path.exists():
    try:
        raw = json.loads(cfg_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是对象")
        # 审查修复#7：config.json 是权威来源，直接赋值（setdefault 会让残留 env 永久压住管理页新 Key）
        for k in _ALLOWED_KEYS & raw.keys():
            if isinstance(raw[k], str) and raw[k]:
                os.environ[k] = raw[k]
    except Exception as e:
        # 审查修复#4：文件存在但损坏 = 配置态异常，fail-fast 而不是带病启动（假健康）
        print(f"[amap-wrapper] config.json 无效（{e}），拒绝启动", file=sys.stderr)
        sys.exit(1)

# 2) amap 在 import 时固化 API key（server.py 模块级 AMAP_MAPS_API_KEY = get_api_key()），所以必须先注入再 import
from amap_mcp_server import mcp

host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
port = int(os.environ.get("MCP_HTTP_PORT", "8323"))
mcp.settings.host = host
mcp.settings.port = port

mcp.run(transport="streamable-http")
