# zotero wrapper：读 data/config.json 注入 env → import zotero_mcp → kwargs 法起 streamable-http
# zotero-mcp 钉 0.3.1（依赖 mcp 2.x：FastMCP→MCPServer，run() 收 host/port/transport_security kwargs；
# DNS rebinding 防护默认仅放行 localhost，需放行上游 Nginx 传入的网关 Host）
import json
import os
import sys
from pathlib import Path

# 审查修复#3：白名单键——config.json 失陷时不能向进程注入任意 env
_ALLOWED_KEYS = {"ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID", "ZOTERO_LIBRARY_TYPE", "ZOTERO_LOCAL"}

cfg_path = Path(os.environ.get("ZOTERO_CONFIG_FILE", "/app/data/config.json"))
if cfg_path.exists():
    try:
        raw = json.loads(cfg_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("顶层必须是对象")
        # 审查修复#7：config.json 是权威来源，直接赋值（setdefault 会让残留 env 永久压住管理页新配置）
        for k in _ALLOWED_KEYS & raw.keys():
            if isinstance(raw[k], str) and raw[k]:
                os.environ[k] = raw[k]
    except Exception as e:
        # 审查修复#4：文件存在但损坏 = fail-fast，避免 initialize 健康、tools/call 才炸的假健康
        print(f"[zotero-wrapper] config.json 无效（{e}），拒绝启动", file=sys.stderr)
        sys.exit(1)

from zotero_mcp import mcp

from mcp.server.transport_security import TransportSecuritySettings

allowed = ["localhost", "127.0.0.1"]
extra = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

mcp.run(
    transport="streamable-http",
    host=os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_HTTP_PORT", "8324")),
    transport_security=TransportSecuritySettings(allowed_hosts=[*allowed, *extra]),
)
