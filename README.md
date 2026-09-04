# mcp-center

多 MCP 统一网关：**一个域名 + 一套 Token 鉴权 + 一个管理界面**，托管任意多个 MCP 服务。客户端零安装，一条 URL + Bearer Token 接入。

```
                https://mcp.example.com
                          │
              ┌───────────▼────────────┐
              │  Nginx (TLS + 路径分流) │  auth_request → 统一鉴权
              └───┬─────────┬──────┬───┘
          /grok/   │  /codex/│      │  /admin
                  ▼         ▼      ▼
           ┌──────────┐ ┌────────┐ ┌──────────────────┐
           │ GrokSearch│ │CodexMCP│ │ mcp-admin :8330  │
           │   :8321   │ │ :8322  │ │ · Token 签发/吊销│
           │ (fork)    │ │ (fork) │ │ · 上游 API 配置   │
           └──────────┘ └────────┘ │ · 管理界面        │
                                   └──────────────────┘
            新增 MCP = 加一个容器 + 一个 location 块
```

## 特性

- **统一鉴权**：Nginx `auth_request` 子请求 → mcp-admin `/verify`（HMAC-SHA256 Token 摘要存储，明文仅创建时展示一次）；**按路径授权**（一个 Token 只能调指定 MCP）；吊销即时生效；mcp-admin 不可达时 fail-closed
- **统一管理界面**：`/admin` 密码登录（bcrypt + 首设防抢注 + CSRF 防护 + 登录限速），各 MCP 一张配置卡片，上游 API Key 保存即**热生效**（零重启）
- **模型下拉框**：填 API URL/Key 后一键从上游拉取模型列表，下拉选择
- **网络隔离**：每容器独立 Docker 网络，互不可达；端口仅绑 `127.0.0.1`，对外唯一入口 Nginx
- **provider 降级**（CodexMCP）：ChatGPT 登录额度优先，额度耗尽/认证失败自动切第三方 API 重跑（单请求内透明降级，`provider_used` 字段可观察）

## 路径规划

| 路径 | 指向 | 端口 |
|------|------|------|
| `/grok/mcp` | GrokSearch fork（web_search / web_fetch / plan_* 等 13 工具） | 127.0.0.1:8321 |
| `/codex/mcp` | CodexMCP fork（codex 工具，含 provider 降级） | 127.0.0.1:8322 |
| `/admin` | mcp-admin 管理界面 | 127.0.0.1:8330 |
| `/NNNN/mcp` | 未来 MCP（8323+ 递增预留） | 127.0.0.1:83XX |

## 目录结构

```
mcp-center/
├── docker-compose.yml      # 三服务 + 独立网络
├── mcp-admin/
│   ├── app.py              # 自研管理/鉴权服务（FastAPI 单文件）
│   ├── Dockerfile
│   └── requirements.txt
├── groksearch/Dockerfile   # 钉 fork commit SHA 构建
├── codexmcp/Dockerfile     # 钉 fork SHA + Codex CLI (Node)
├── .gitignore              # secrets/data/.env 不入库
└── (运行时生成) data/ secrets/ logs/ workspace/
```

## 部署

依赖：Docker + Compose、Nginx（需 `--with-http_auth_request_module`）、acme.sh（或任意 ACME 客户端）。

> **脱敏说明**：仓库中的域名统一为 `mcp.example.com` / `api.example.com` 占位。部署时请替换：
> - `docker-compose.yml` 的 `MCP_ALLOWED_HOSTS`（CodexMCP 的 Host 校验白名单，填你的真实网关域名）
> - `mcp-admin/app.py` 中 `VERIFY_REALM` 与管理页标题里的域名（仅展示用途）
> - Nginx vhost 的 `server_name` 与证书路径

```bash
# 1. 目录与密钥
mkdir -p data/{grok,codex/home,admin} logs/grok workspace secrets
openssl rand -hex 32 > secrets/token_hmac_pepper   # Token HMAC pepper（0600）
openssl rand -hex 16 > /tmp/setup_key              # 管理密码首设密钥
chmod -R 600 secrets/*; chmod 700 secrets
chown -R 1000:1000 data logs workspace

# 2. .env（仅非敏感项；上游 API Key 一律走管理界面，不进 env）
cat > .env <<EOF
ADMIN_SETUP_KEY=$(cat /tmp/setup_key)
EOF

# 3. 构建启动
docker compose build --no-cache
docker compose up -d

# 4. Nginx vhost（参考下方配置要点）+ ACME 证书
# 5. 浏览器打开 https://<域名>/admin → 输入 ADMIN_SETUP_KEY + 设置管理密码
#    → 各 MCP 配置卡片填上游 API → 生成客户端 Token
```

### Nginx 配置要点

```nginx
# 每个 MCP 一个 location（新增 MCP = 复制一块）
location /grok/ {
    auth_request /_mcp_verify;
    set $mcp_acl_path "/grok";          # 授权资源名（防伪造）
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    auth_request_set $auth_token_id $upstream_http_x_auth_token_id;
    proxy_set_header X-Authenticated-Token-Id $auth_token_id;
    proxy_set_header Authorization "";   # 鉴权后不向下游透传 Token
    proxy_buffering off;                 # SSE 流式必需
    proxy_read_timeout 3600s;            # 长任务
    proxy_pass http://127.0.0.1:8321/;   # 尾斜杠剥前缀
}

location = /_mcp_verify {
    internal;
    proxy_pass http://127.0.0.1:8330/verify;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-MCP-Path $mcp_acl_path;
    proxy_connect_timeout 2s;
    proxy_read_timeout 5s;
}
```

## 客户端接入

接入前先在管理界面（`/admin` → 新建 Token）生成 Token，勾选该客户端需要的路径权限。以下按客户端逐条给出配置。

### Claude Code（每条 MCP 一个命令）

```bash
# 搜索（mcp-grok）
claude mcp add --transport http mcp-grok "https://<域名>/grok/mcp" \
  --header "Authorization: Bearer <token>" --scope user

# Codex 协作（mcp-codex）
claude mcp add --transport http mcp-codex "https://<域名>/codex/mcp" \
  --header "Authorization: Bearer <token>" --scope user

# 验证：两条都应显示 ✔ Connected
claude mcp list
```

### Codex CLI（Token 走环境变量，避免明文进配置文件）

```bash
echo 'export MCP_TOKEN=<token>' >> ~/.bashrc && source ~/.bashrc
codex mcp add mcp-grok --url "https://<域名>/grok/mcp" --bearer-token-env-var MCP_TOKEN
codex mcp add mcp-codex --url "https://<域名>/codex/mcp" --bearer-token-env-var MCP_TOKEN
codex mcp list   # 验证
```

### Hermes Agent（`~/.hermes/config.yaml` 的 `mcp_servers` 段）

每个 MCP 一个条目；**有 `url` 字段即 HTTP 型**（stdio 型才写 `command`）：

```yaml
mcp_servers:
  mcp-grok:
    url: "https://<域名>/grok/mcp"
    headers:
      Authorization: "Bearer <token>"
    timeout: 180
  mcp-codex:
    url: "https://<域名>/codex/mcp"
    headers:
      Authorization: "Bearer <token>"
    timeout: 300
```

改完重启生效：`systemctl --user restart hermes-gateway`（Docker 部署则重启对应容器）。
迁移提示：原来 stdio 型条目（`command: uvx ...` 形态）直接换成上面的 url + headers 写法即可，上游 API Key 不再写在 Hermes 配置里，集中到管理界面维护。

### Gemini CLI（`~/.gemini/settings.json`）

```json
{ "mcpServers": {
    "mcp-grok": {
      "httpUrl": "https://<域名>/grok/mcp",
      "headers": { "Authorization": "Bearer <token>" } } } }
```

### Cursor / Cherry Studio / ChatBox 等 GUI 客户端

设置 → MCP → 添加服务器 → 类型选 **Streamable HTTP** → URL 填端点地址，请求头加 `Authorization: Bearer <token>`。

### Python 代码集成（官方 SDK）

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

headers = {"Authorization": "Bearer <token>"}

async with streamablehttp_client("https://<域名>/grok/mcp", headers=headers) as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        result = await s.call_tool("web_search", {"query": "..."})
```

Token 格式 `mcp_v2_<id>_<secret>`，仅创建时展示一次。建议一机一 Token；吊销即时生效。

## 新增一个 MCP

1. 写 Dockerfile（或用现成镜像），绑 `127.0.0.1:83XX`，加入 compose（独立网络）
2. Nginx 加一个 `location /NNNN/` 块（复制现有块，改前缀/端口/ACL 资源名）
3. `mcp-admin/app.py` 的 `MCP_CATALOG` 加一条（管理页即出现配置卡片）
4. Token 勾选新路径即授权

## CodexMCP 的 provider 降级

环境变量 `CODEX_PROVIDER_ORDER`（默认 `chatgpt,custom`）决定尝试顺序：

- **chatgpt** = 容器内 `codex login` 的登录额度（`docker exec -it <容器> codex login`）
- **custom** = 管理页配置的第三方 API（OpenAI Responses 兼容端点）

命中额度/认证类失败（usage limit / 401 / 429 / not logged in 等）时单请求内自动降级重跑；跨 provider 不续会话。只想走第三方设 `CODEX_PROVIDER_ORDER=custom`。

> 注意：不要把一台机器的 `~/.codex/auth.json` 复制到容器共用——refresh token 轮换会互踢登录。

## 升级与回滚

镜像 tag 制度：每次构建打 `:vN`，旧 tag 不删。fork 升级 = 改 Dockerfile 里的 commit SHA → `build --no-cache` → `up -d`；回滚 = compose 里 image 改回旧 tag。

## 安全设计清单

- Token 库只存 HMAC-SHA256 摘要（pepper 独立 0600 文件），恒定时间比较，抗时序探测
- 授权资源名由 Nginx internal 子请求注入，客户端伪造 `X-MCP-Path` 无效
- tokens.json 原子写（tmp + fsync + os.replace），单 Uvicorn worker 单写者
- 管理面：bcrypt 密码 + setup key 首设防抢注 + session 存 SID 的 HMAC + CSRF（自定义头 + Origin 校验）+ 登录失败退避 + Nginx 分档限速
- 上游 Key 落 data/ 卷内 0600 文件，`.env` 不含密钥，`docker inspect` 无泄漏面
- fail-closed：鉴权服务不可达 → 全部请求拒绝

## License

MIT
