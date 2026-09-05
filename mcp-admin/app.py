#!/usr/bin/env python3
"""mcp-admin — 多 MCP 统一网关管理/鉴权服务

职责（v2 架构核心）：
  1. /verify   ：Nginx auth_request 子请求校验 Bearer Token（HMAC 摘要 + 按路径授权），fail-closed
  2. 管理界面  ：/admin（bcrypt 密码 + ADMIN_SETUP_KEY 首设防抢注）
  3. Token 管理：签发（mcp_v2_<id>_<secret>，明文仅展示一次）/ 吊销 / 删除
  4. 上游配置  ：每 MCP 一张配置卡片 → data/<mcp>/config.json（热重载或提示手动重启）

设计约束：
  - 单 Uvicorn worker（tokens.json 单写者前提）
  - tokens.json 原子写（tmp + fsync + os.replace）
  - request_count 内存累加，定期 flush（默认 60s）
  - API Key 等敏感值仅存 data/ 卷内 0600 文件，.env 只留非敏感项
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as pysecrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt
os.umask(0o077)  # 审查修复#3：进程级屏蔽 other/group 读（tokens/admin/sessions 自动 0600）

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# ── 路径与常量 ──────────────────────────────────────────────
DATA_DIR = Path(os.getenv("ADMIN_DATA_DIR", "/app/data"))
ADMIN_DIR = DATA_DIR / "admin"
TOKENS_FILE = ADMIN_DIR / "tokens.json"
ADMIN_FILE = ADMIN_DIR / "admin.json"
SESSIONS_FILE = ADMIN_DIR / "sessions.json"
PEPPER_FILE = Path(os.getenv("ADMIN_PEPPER_FILE", "/app/secrets/token_hmac_pepper"))

TOKEN_PREFIX = "mcp_v2"
SESSION_TTL = 12 * 3600  # 管理会话 12h
FLUSH_INTERVAL = 60.0    # request_count flush 周期
VERIFY_REALM = 'Bearer realm="mcp.example.com"'

# 管理页可配置的 MCP 集合（新增 MCP：此处 + Nginx location + compose 各加一条）
MCP_CATALOG = {
    "grok": {
        "label": "GrokSearch",
        "desc": "Grok 深度搜索 + Tavily 抓取 + Firecrawl 托底",
        "path": "/grok",
        "container": "mcp-groksearch",
        "restart_needed": False,  # config.json mtime 热重载
        "config_fields": [
            {"key": "GROK_API_URL", "label": "Grok API URL", "secret": False, "placeholder": "https://api.x.ai/v1 或中转地址"},
            {"key": "GROK_API_KEY", "label": "Grok API Key", "secret": True, "placeholder": "sk-..."},
            {"key": "GROK_MODEL", "label": "默认模型", "secret": False, "placeholder": "grok-4-fast", "type": "model_select"},
            {"key": "TAVILY_API_KEY", "label": "Tavily API Key", "secret": True, "placeholder": "tvly-..."},
            {"key": "FIRECRAWL_API_KEY", "label": "Firecrawl API Key", "secret": True, "placeholder": "fc-..."},
        ],
    },
    "codex": {
        "label": "CodexMCP",
        "desc": "Codex CLI 协作（第三方 API 表单化配置，保存即热生效）",
        "path": "/codex",
        "container": "mcp-codexmcp",
        "restart_needed": False,
        "config_fields": [
            {"key": "CODEX_BASE_URL", "label": "第三方 API Base URL", "secret": False, "placeholder": "https://api.example.com/v1（OpenAI Responses 兼容）"},
            {"key": "CODEX_API_KEY", "label": "API Key", "secret": True, "placeholder": "sk-..."},
            {"key": "CODEX_MODEL", "label": "默认模型", "secret": False, "placeholder": "gpt-5.5", "type": "model_select"},
            {"key": "CODEX_MAX_CONCURRENCY", "label": "并发上限", "secret": False, "placeholder": "2"},
        ],
    },
    "amap": {
        "label": "Amap Maps",
        "desc": "高德地图（地理编码/路线/天气/POI 16 工具，sugarforever Python 版）",
        "path": "/amap",
        "container": "mcp-amapmcp",
        # amap 在模块 import 时固化 API Key（server.py 模块级读取）→ 改 Key 需重启容器
        "restart_needed": True,
        "config_fields": [
            {"key": "AMAP_MAPS_API_KEY", "label": "高德 Web 服务 API Key", "secret": True, "placeholder": "在高德开放平台控制台创建（Web 服务类型）"},
        ],
    },
    "zotero": {
        "label": "Zotero",
        "desc": "文献库检索/元数据/全文（kujenga/zotero-mcp，Web API 模式）",
        "path": "/zotero",
        "container": "mcp-zoteromcp",
        # get_zotero_client 每次调用读 os.environ，但 env 由 wrapper 启动时注入后进程内固化 → 改配置需重启容器
        "restart_needed": True,
        "config_fields": [
            {"key": "ZOTERO_API_KEY", "label": "Zotero API Key", "secret": True, "placeholder": "zotero.org/settings/keys 创建"},
            {"key": "ZOTERO_LIBRARY_ID", "label": "Library ID（用户 ID）", "secret": False, "placeholder": "如 20242038"},
            {"key": "ZOTERO_LIBRARY_TYPE", "label": "Library 类型", "secret": False, "placeholder": "user 或 group"},
        ],
    },
}

from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(_app):
    yield
    try:
        STORE.maybe_flush(force=True)  # 审查修复#7：退出前强制 flush 使用计数
    except Exception:
        pass

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

# ── 基础设施：原子 JSON 读写 / pepper ──────────────────────
def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dirfd = os.open(path.parent, os.O_DIRECTORY)
        os.fsync(dirfd)
        os.close(dirfd)
    except OSError:
        pass


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _get_pepper() -> bytes:
    if PEPPER_FILE.exists():
        return PEPPER_FILE.read_bytes().strip()
    # 首次启动自动生成（部署时已由宿主机预置；此处兜底）
    PEPPER_FILE.parent.mkdir(parents=True, exist_ok=True)
    val = pysecrets.token_hex(32)
    PEPPER_FILE.write_text(val)
    try:
        os.chmod(PEPPER_FILE, 0o600)
    except OSError:
        pass
    return val.encode()


PEPPER = _get_pepper()
_DUMMY_HMAC = hmac.new(PEPPER, b"<dummy>", hashlib.sha256).hexdigest()


def _hmac_secret(secret: str) -> str:
    return hmac.new(PEPPER, secret.encode(), hashlib.sha256).hexdigest()


# ── Token 库（内存缓存 + mtime 感知重载）──────────────────
class TokenStore:
    def __init__(self):
        self._tokens: dict[str, dict] = {}
        self._mtime_ns = (0, 0, 0)
        self._dirty_counts: dict[str, int] = {}
        self._last_flush = 0.0
        self._reload_if_needed()

    def _reload_if_needed(self) -> None:
        try:
            st = TOKENS_FILE.stat()
        except FileNotFoundError:
            ADMIN_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(TOKENS_FILE, {"schema_version": 2, "tokens": []})
            self._tokens = {}
            self._mtime_ns = (0, 0, 0)
            return
        if (st.st_mtime_ns, st.st_size, st.st_ino) == self._mtime_ns:
            return
        # 审查修复#7：重载前把未 flush 的使用计数并入磁盘数据，防外部写导致计数回零
        data = _load_json(TOKENS_FILE, {"schema_version": 2, "tokens": []})
        for t in data.get("tokens", []):
            rec = self._tokens.get(t["id"])
            if rec and t.get("request_count", 0) < rec.get("request_count", 0):
                t["request_count"] = rec["request_count"]
                t["last_used_at"] = rec.get("last_used_at")
        self._tokens = {t["id"]: t for t in data.get("tokens", [])}
        self._mtime_ns = (st.st_mtime_ns, st.st_size, st.st_ino)

    def _persist(self) -> None:
        _atomic_write_json(
            TOKENS_FILE,
            {"schema_version": 2, "tokens": list(self._tokens.values())},
        )
        st = TOKENS_FILE.stat()
        self._mtime_ns = (st.st_mtime_ns, st.st_size, st.st_ino)

    def get(self, token_id: str) -> Optional[dict]:
        self._reload_if_needed()
        return self._tokens.get(token_id)

    def create(self, name: str, paths: list[str], expires_at: Optional[str]) -> tuple[dict, str]:
        self._reload_if_needed()
        token_id = pysecrets.token_hex(6)
        secret = pysecrets.token_hex(32)
        full = f"{TOKEN_PREFIX}_{token_id}_{secret}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec = {
            "id": token_id,
            "name": name,
            "token_prefix": f"{TOKEN_PREFIX}_{token_id}_{secret[:4]}",
            "secret_hmac": _hmac_secret(secret),
            "enabled": True,
            "permissions": {"paths": paths},
            "created_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
            "last_used_at": None,
            "request_count": 0,
        }
        self._tokens[token_id] = rec
        self._persist()
        return rec, full

    def revoke(self, token_id: str, revoked: bool) -> bool:
        self._reload_if_needed()
        rec = self._tokens.get(token_id)
        if not rec:
            return False
        rec["enabled"] = not revoked
        rec["revoked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds") if revoked else None
        self._persist()
        return True

    def delete(self, token_id: str) -> bool:
        self._reload_if_needed()
        if token_id not in self._tokens:
            return False
        del self._tokens[token_id]
        self._persist()
        return True

    def count_use(self, token_id: str) -> None:
        rec = self._tokens.get(token_id)
        if rec is not None:
            rec["request_count"] = int(rec.get("request_count", 0)) + 1
            rec["last_used_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._dirty_counts[token_id] = self._dirty_counts.get(token_id, 0) + 1

    def maybe_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (not self._dirty_counts or now - self._last_flush < FLUSH_INTERVAL):
            return
        if self._dirty_counts:
            self._persist()
            self._dirty_counts.clear()
        self._last_flush = now


STORE = TokenStore()


@app.middleware("http")
async def _flush_middleware(request: Request, call_next):
    resp = await call_next(request)
    try:
        STORE.maybe_flush()
    except Exception:
        pass
    return resp


# ── /verify：Nginx auth_request 子请求端点 ─────────────────
def _unauthorized(err: str = "invalid_token") -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": f'{VERIFY_REALM}, error="{err}"'})


@app.api_route("/verify", methods=["GET", "POST", "DELETE", "HEAD"])
async def verify(request: Request):
    # 仅接受本机回环 / Docker 网关子请求（宿主机 Nginx 经端口映射进容器时 peer 为网桥 IP）
    peer = request.client.host if request.client else ""
    if not (peer.startswith("127.") or peer.startswith("172.") or peer == "::1"):
        return Response(status_code=404)

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return _unauthorized("missing_token")
    token = auth[len("Bearer "):].strip()

    # token 形如 mcp_v2_<id>_<secret>；前缀本身含下划线，故按前缀长度切分而非 split
    if not token.startswith(TOKEN_PREFIX + "_"):
        return _unauthorized("malformed_token")
    rest = token[len(TOKEN_PREFIX) + 1:]
    token_id, _, secret = rest.partition("_")
    if not token_id or not secret:
        return _unauthorized("malformed_token")
    record = STORE.get(token_id)

    # 恒定时间比较；不存在的 id 与错误 secret 同路径（抗时序探测）
    candidate = _hmac_secret(secret)
    expected = record["secret_hmac"] if record else _DUMMY_HMAC
    if not hmac.compare_digest(candidate, expected):
        return _unauthorized("invalid_token")

    if not record["enabled"] or record.get("revoked_at"):
        return _unauthorized("token_revoked")

    exp = record.get("expires_at")
    if exp:
        try:
            if datetime.now(timezone.utc) >= datetime.fromisoformat(exp):
                return _unauthorized("token_expired")
        except ValueError:
            return _unauthorized("token_expired")

    # 按路径授权：资源名由 Nginx 注入（X-MCP-Path），非客户端可控
    resource = (request.headers.get("x-mcp-path") or "").strip()
    if not resource:
        return Response(status_code=403)
    granted = record.get("permissions", {}).get("paths", [])
    ok = any(
        resource == g or resource.startswith(g.rstrip("/") + "/")
        for g in granted if g
    )
    if not ok:
        return Response(status_code=403)

    STORE.count_use(token_id)
    return Response(status_code=204, headers={"X-Auth-Token-Id": token_id})


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "1.0.0", "service": "mcp-admin"}


# ── 管理端：会话 / 登录 ────────────────────────────────────
def _sid_hmac(sid: str) -> str:
    """审查修复#3：会话文件只存 SID 的 HMAC（泄漏不可直接冒用）"""
    return hmac.new(PEPPER, ("sid:" + sid).encode(), hashlib.sha256).hexdigest()


def _load_sessions() -> dict:
    return _load_json(SESSIONS_FILE, {})


def _save_sessions(s: dict) -> None:
    _atomic_write_json(SESSIONS_FILE, s)


def _admin_exists() -> bool:
    d = _load_json(ADMIN_FILE, {})
    return bool(d.get("password_hash"))


def _session_valid(request: Request) -> bool:
    sid = request.cookies.get("mcp_admin_session", "")
    if not sid:
        return False
    s = _load_sessions()
    rec = s.get(_sid_hmac(sid))
    if not rec:
        return False
    if time.time() - rec["ts"] > SESSION_TTL:
        s.pop(_sid_hmac(sid), None)
        _save_sessions(s)
        return False
    return True


_LOGIN_FAILS: dict[str, list[float]] = {}


def _login_throttled(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 600]
    _LOGIN_FAILS[ip] = fails
    return len(fails) >= 5


def _record_login_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def _client_ip(request: Request) -> str:
    """审查修复#9：管理 API 经宿主 Nginx 反代，取 X-Real-IP（Nginx 注入，外部不可伪造覆盖）"""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")



def _csrf_ok(request: Request) -> bool:
    """审查修复#3：写接口要求 X-Requested-With 自定义头 + Origin 同源（跨站表单无法伪造）"""
    if request.headers.get("x-requested-with") != "fetch":
        return False
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        try:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != host:
                return False
        except Exception:
            return False
    return True

@app.post("/admin/api/login")
async def admin_login(request: Request):
    ip = _client_ip(request)
    if _login_throttled(ip):
        return JSONResponse({"error": "尝试次数过多，请 10 分钟后再试"}, status_code=429)
    body = await request.json()

    if not _admin_exists():
        # 首次设置：须同时提交 ADMIN_SETUP_KEY（防公网抢注）
        setup_key = (body.get("setup_key") or "").strip()
        expected = os.getenv("ADMIN_SETUP_KEY", "")
        if not expected or not hmac.compare_digest(setup_key, expected):
            _record_login_fail(ip)
            return JSONResponse({"error": "初始化密钥错误", "need_setup": True}, status_code=401)
        pw = (body.get("password") or "")
        if len(pw) < 4:
            return JSONResponse({"error": "密码至少 4 位"}, status_code=400)
        ADMIN_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            ADMIN_FILE,
            {"password_hash": bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        )
    else:
        pw = (body.get("password") or "")
        rec = _load_json(ADMIN_FILE, {})
        if not bcrypt.checkpw(pw.encode(), rec.get("password_hash", "").encode()):
            _record_login_fail(ip)
            return JSONResponse({"error": "密码错误"}, status_code=401)

    sid = pysecrets.token_hex(24)
    s = _load_sessions()
    s[_sid_hmac(sid)] = {"ts": time.time()}
    _save_sessions(s)
    _LOGIN_FAILS.pop(ip, None)  # 审查修复#9：登录成功清除失败计数
    resp = JSONResponse({"ok": True})
    resp.set_cookie("mcp_admin_session", sid, httponly=True, samesite="lax", max_age=SESSION_TTL, secure=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    sid = request.cookies.get("mcp_admin_session", "")
    if sid:
        s = _load_sessions()
        s.pop(_sid_hmac(sid), None)
        _save_sessions(s)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mcp_admin_session")
    return resp


# ── 管理端：Token CRUD ─────────────────────────────────────
@app.get("/admin/api/tokens")
async def list_tokens(request: Request):
    if not _session_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    STORE._reload_if_needed()
    STORE.maybe_flush(force=True)
    out = []
    for t in STORE._tokens.values():
        out.append({k: v for k, v in t.items() if k != "secret_hmac"})
    return {"tokens": out, "mcp_catalog": {k: {"label": v["label"], "path": v["path"]} for k, v in MCP_CATALOG.items()}}


@app.post("/admin/api/tokens")
async def create_token(request: Request):
    if not _session_valid(request) or not _csrf_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip() or "unnamed"
    paths = body.get("paths") or []
    valid = [p for p in paths if p in {v["path"] for v in MCP_CATALOG.values()}]
    if not valid:
        return JSONResponse({"error": "至少选择一个有效路径"}, status_code=400)
    expires_at = body.get("expires_at") or None
    if expires_at:
        try:
            dt = datetime.fromisoformat(expires_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            expires_at = dt.isoformat(timespec="seconds")
        except ValueError:
            return JSONResponse({"error": "expires_at 格式无效（ISO 8601）"}, status_code=400)
    rec, full = STORE.create(name, valid, expires_at)
    return {"token": {k: v for k, v in rec.items() if k != "secret_hmac"}, "full_token": full}


@app.post("/admin/api/tokens/{token_id}/toggle")
async def toggle_token(token_id: str, request: Request):
    if not _session_valid(request) or not _csrf_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    ok = STORE.revoke(token_id, bool(body.get("revoked", True)))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.delete("/admin/api/tokens/{token_id}")
async def delete_token(token_id: str, request: Request):
    if not _session_valid(request) or not _csrf_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ok = STORE.delete(token_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ── 管理端：各 MCP 上游配置卡片 ─────────────────────────────
def _mcp_config_file(mcp_name: str) -> Path:
    return DATA_DIR / mcp_name / "config.json"


def _read_mcp_config(mcp_name: str) -> dict:
    return _load_json(_mcp_config_file(mcp_name), {})


@app.get("/admin/api/config/{mcp_name}")
async def get_mcp_config(mcp_name: str, request: Request):
    if not _session_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if mcp_name not in MCP_CATALOG:
        return JSONResponse({"error": "unknown mcp"}, status_code=404)
    meta = MCP_CATALOG[mcp_name]
    cfg = _read_mcp_config(mcp_name)
    # secret 字段只回显是否已设置 + 掩码；审查修复#8：短值（≤4 字符）完整回显=泄露，统一只给掩码形态
    view = {}
    for f in meta["config_fields"]:
        val = cfg.get(f["key"], "")
        if f["secret"]:
            view[f["key"]] = ("●" + val[-4:]) if val else ""
        else:
            view[f["key"]] = val
    return {"mcp": meta, "config": view}


@app.post("/admin/api/models/{mcp_name}")
async def fetch_upstream_models(mcp_name: str, request: Request):
    """从上游拉取模型列表（GrokSearch：GET <url>/models，Bearer Key）。服务端代拉避免 CORS。"""
    if not _session_valid(request) or not _csrf_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    field_map = {"grok": ("GROK_API_URL", "GROK_API_KEY"), "codex": ("CODEX_BASE_URL", "CODEX_API_KEY")}
    if mcp_name not in field_map:
        return JSONResponse({"error": "该 MCP 不支持模型拉取"}, status_code=404)
    url_key, key_field = field_map[mcp_name]
    body = await request.json()
    cfg = _read_mcp_config(mcp_name)
    api_url = (body.get(url_key) or cfg.get(url_key) or "").strip().rstrip("/")
    api_key = (body.get(key_field) or "").strip()
    if api_key.startswith("\u25cf"):  # 掩码形态 → 用已存值
        api_key = cfg.get(key_field, "")
    if not api_key:
        api_key = cfg.get(key_field, "")
    if not api_url or not api_key:
        return JSONResponse({"error": "请先填写并保存 API URL 与 Key"}, status_code=400)
    import urllib.request, urllib.error
    req = urllib.request.Request(
        f"{api_url}/models",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return JSONResponse({"error": f"上游返回 HTTP {e.code}：{e.read().decode(errors='replace')[:200]}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"连接失败：{type(e).__name__} {e}"}, status_code=502)
    models = [
        item.get("id") for item in (data or {}).get("data", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    models.sort()
    return {"models": models, "count": len(models)}


@app.post("/admin/api/config/{mcp_name}")
async def set_mcp_config(mcp_name: str, request: Request):
    if not _session_valid(request) or not _csrf_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if mcp_name not in MCP_CATALOG:
        return JSONResponse({"error": "unknown mcp"}, status_code=404)
    body = await request.json()
    meta = MCP_CATALOG[mcp_name]
    # 审查修复#3：只保留 config_fields 声明过的键——config.json 是 wrapper 的 env 注入源，
    # 不能混入历史遗留/手工添加的任意键（消费者侧 wrapper 已另做白名单双重收口）
    cfg = {}
    for f in meta["config_fields"]:
        if f["key"] in body:
            new = (body[f["key"]] or "").strip()
            # secret 字段留空或掩码形态（●开头）= 不变
            if f["secret"] and (not new or new.startswith("●")):
                new = (_read_mcp_config(mcp_name).get(f["key"]) or "")
            if new:
                cfg[f["key"]] = new
    _mcp_config_file(mcp_name).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_mcp_config_file(mcp_name), cfg)

    # codex 专属：把表单值物化为 ~/.codex/config.toml + provider_api_key（容器挂载卷内，热生效）
    if mcp_name == "codex":
        msg = _render_codex_files(cfg)
        if msg:
            return JSONResponse({"error": msg}, status_code=400)

    return {"ok": True, "restart_needed": meta["restart_needed"]}


def _render_codex_files(cfg: dict) -> str:
    """把管理页 codex 表单写入容器挂载卷：
    - data/codex/home/.codex/config.toml（model_providers + auth.command 读 key 文件）
    - data/codex/home/.codex/provider_api_key（0600）
    auth.command 每次请求执行 cat → 改 Key 零重启热生效（实测 0.153.0）。
    返回错误信息（空串=成功）。"""
    base_url = (cfg.get("CODEX_BASE_URL") or "").strip().rstrip("/")
    model = (cfg.get("CODEX_MODEL") or "gpt-5.5").strip()
    api_key = (cfg.get("CODEX_API_KEY") or "").strip()
    if not base_url:
        return "请填写第三方 API Base URL"
    if not api_key:
        return "请填写 API Key"
    codex_home = DATA_DIR / "codex" / "home" / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    toml = f"""# 由 mcp-admin 管理页生成（{datetime.now(timezone.utc).isoformat(timespec="seconds")}），手工改动会被覆盖
# provider 策略：不设默认 model_provider —— chatgpt（登录额度）为默认通道；
# custom（第三方保底）由 codexmcp 在额度/认证失败时经 -c model_provider=custom 运行时启用
model = "{model}"

[model_providers.custom]
name = "Custom Provider (mcp-admin)"
base_url = "{base_url}"
wire_api = "responses"

[model_providers.custom.auth]
command = "cat"
args = ["/home/node/.codex/provider_api_key"]
"""
    (codex_home / "config.toml").write_text(toml, encoding="utf-8")
    key_file = codex_home / "provider_api_key"
    key_file.write_text(api_key + "\n", encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
        os.chmod(codex_home / "config.toml", 0o600)
    except OSError:
        pass
    return ""


@app.get("/admin/api/codex/status")
async def codex_login_status(request: Request):
    """Codex 凭据状态：auth.json（ChatGPT 登录）或 config.toml+key（第三方 API）"""
    if not _session_valid(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    codex_home = DATA_DIR / "codex" / "home" / ".codex"
    auth = _load_json(codex_home / "auth.json", None)
    mode = None
    if isinstance(auth, dict):
        mode = auth.get("auth_mode") or "logged_in"
    cfg = _read_mcp_config("codex")
    third_party_ready = bool(cfg.get("CODEX_BASE_URL") and cfg.get("CODEX_API_KEY"))
    return {
        "chatgpt_login": mode,          # None=未登录；chatgpt/apikey=已登录模式
        "third_party_ready": third_party_ready,
        "base_url": (cfg.get("CODEX_BASE_URL") or "")[:60],
        "model": cfg.get("CODEX_MODEL") or "",
    }


# ── 管理界面（单文件 HTML/JS）──────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin", status_code=302)


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP 网关管理</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#2a2f3a;--fg:#e6e9ef;--dim:#8b93a3;--acc:#4f8cff;--ok:#3fb96f;--warn:#e0a23c;--err:#e05c5c}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;background:#223; color:var(--dim)}
.badge.on{background:#123524;color:var(--ok)}.badge.off{background:#3a1a1a;color:var(--err)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500}
input,select,button{font:inherit;border-radius:6px;border:1px solid var(--line);background:#10131a;color:var(--fg);padding:7px 10px}
input:focus,select:focus{outline:none;border-color:var(--acc)}
button{cursor:pointer;background:var(--acc);border-color:var(--acc);color:#fff}
button.ghost{background:transparent;color:var(--dim)}
button.danger{background:transparent;border-color:var(--err);color:var(--err)}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
label{font-size:12px;color:var(--dim);display:block;margin:10px 0 4px}
.field input{width:100%}
.hint{font-size:12px;color:var(--warn);margin-top:8px}
.toast{position:fixed;bottom:20px;right:20px;background:var(--card);border:1px solid var(--acc);padding:12px 18px;border-radius:8px;font-size:13px;display:none;max-width:420px;word-break:break-all}
.mono{font-family:ui-monospace,monospace;font-size:12px}
a{color:var(--acc);text-decoration:none}
</style></head><body><div class="wrap">
<h1>MCP 网关管理 <span class="badge" id="ver"></span></h1>
<div class="sub">mcp.example.com · 统一 Token 鉴权与上游配置</div>

<div class="card" id="loginCard" style="display:none">
<h2>登录</h2>
<div id="setupHint" class="hint" style="display:none">首次设置：需同时输入初始化密钥（ADMIN_SETUP_KEY，见服务器 .env）</div>
<label>初始化密钥（仅首次）<input id="setupKey" placeholder="ADMIN_SETUP_KEY" autocomplete="off"></label>
<label>管理密码<div class="row" style="margin:0"><input id="pw" type="password" autocomplete="off" data-secret="1" style="flex:1"><button class="ghost" type="button" onclick="togglePw(this)" title="显示/隐藏">👁</button></div></label>
<div class="row"><button onclick="login()">登录 / 首次设置</button></div>
</div>

<div id="main" style="display:none">
<div class="row" style="justify-content:space-between">
  <div class="row"><button onclick="loadTokens()">刷新</button><button class="ghost" onclick="logout()">退出</button></div>
</div>

<div class="card"><h2>客户端 Token</h2>
<table><thead><tr><th>名称</th><th>前缀</th><th>路径权限</th><th>调用数</th><th>最近使用</th><th>状态</th><th></th></tr></thead>
<tbody id="tokRows"></tbody></table>
<hr style="border-color:var(--line);border-top:none;margin:16px 0">
<h2 style="margin-top:0">新建 Token</h2>
<label>名称<input id="tName" placeholder="如 agent-laptop"></label>
<label>路径权限（多选）</label><div class="row" id="pathChecks"></div>
<div class="row"><button onclick="createToken()">生成 Token</button></div>
<div id="newTok" class="mono" style="display:none;margin-top:10px;color:var(--ok);word-break:break-all"></div>
</div>

<div id="configCards"></div>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
let CATALOG={};
const $=id=>document.getElementById(id);
function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.style.borderColor=ok?'var(--ok)':'var(--err)';t.style.display='block';setTimeout(()=>t.style.display='none',6000)}
// 密钥输入框显示/隐藏切换（按钮必须 type=button 防触发表单默认行为）
function togglePw(btn){const inp=btn.parentElement.querySelector('input');if(!inp)return;const show=inp.type==='password';inp.type=show?'text':'password';btn.textContent=show?'🙈':'👁';btn.title=show?'隐藏':'显示';}
async function api(path,opts){const r=await fetch(path,{headers:{'Content-Type':'application/json','X-Requested-With':'fetch'},...opts});if(r.status===401){showLogin();throw 401}return r}

function showLogin(){$('loginCard').style.display='block';$('main').style.display='none'}
async function login(){
  const body={password:$('pw').value};
  if($('setupKey').value)body.setup_key=$('setupKey').value;
  const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){toast(d.error||'登录失败',false);
    if(d.need_setup)$('setupHint').style.display='block';return}
  enterMain();
}
async function logout(){await api('/admin/api/logout',{method:'POST'});showLogin()}

async function enterMain(){
  $('loginCard').style.display='none';$('main').style.display='block';
  const r=await api('/healthz');$('ver').textContent=(await r.json()).version;
  await loadTokens();await loadConfigs();
}

async function loadTokens(){
  const r=await api('/admin/api/tokens');const d=await r.json();CATALOG=d.mcp_catalog;
  renderPathChecks();
  $('tokRows').innerHTML=d.tokens.map(t=>`<tr>
    <td>${esc(t.name)}</td><td class="mono">${t.token_prefix}…</td>
    <td>${(t.permissions.paths||[]).join(', ')}</td>
    <td>${t.request_count}</td><td>${t.last_used_at?String(t.last_used_at).slice(0,19).replace('T',' '):'—'}</td>
    <td><span class="badge ${t.enabled?'on':'off'}">${t.enabled?'启用':'停用'}</span></td>
    <td><button class="ghost" onclick="toggleTok('${t.id}',${t.enabled})">${t.enabled?'停用':'启用'}</button>
        <button class="danger" onclick="delTok('${t.id}')">删除</button></td></tr>`).join('');
}
function renderPathChecks(){
  $('pathChecks').innerHTML=Object.values(CATALOG).map(c=>`<label style="margin:0"><input type="checkbox" value="${c.path}"> ${c.path}（${c.label}）</label>`).join('');
}
async function createToken(){
  const paths=[...document.querySelectorAll('#pathChecks input:checked')].map(i=>i.value);
  const r=await api('/admin/api/tokens',{method:'POST',body:JSON.stringify({name:$('tName').value,paths})});
  const d=await r.json();
  if(!r.ok){toast(d.error,false);return}
  $('newTok').style.display='block';
  $('newTok').innerHTML='Token（仅此一次展示）：<span class="mono" id="tokFull">'+esc(d.full_token)+'</span> <button class="ghost" style="padding:2px 10px" onclick="copyTok()">复制</button>';
  copyTok(true);
  toast('Token 已生成');loadTokens();
}
async function copyTok(silent){
  const t=$('tokFull')?.textContent||'';
  try{
    await navigator.clipboard.writeText(t);
    if(!silent)toast('已复制到剪贴板');
    else toast('Token 已生成并复制到剪贴板');
  }catch(e){
    // 剪贴板 API 不可用（非安全上下文等）→ 退回选中提示
    const r=document.createRange();r.selectNodeContents($('tokFull'));
    const s=getSelection();s.removeAllRanges();s.addRange(r);
    toast('已全选，请 Ctrl+C 复制');
  }
}
async function toggleTok(id,enabled){await api(`/admin/api/tokens/${id}/toggle`,{method:'POST',body:JSON.stringify({revoked:enabled})});loadTokens()}
async function delTok(id){if(!confirm('确认删除该 Token？'))return;await api(`/admin/api/tokens/${id}`,{method:'DELETE'});loadTokens()}

async function loadConfigs(){
  const cards=[];
  for(const[name,meta]of Object.entries(CATALOG)){
    const r=await api(`/admin/api/config/${name}`);const d=await r.json();
    const fields=d.mcp.config_fields.map(f=>{
      if(f.type==='model_select')return `<div class="field"><label>${f.label}（填写 URL/Key 并保存后可拉取列表）</label>
      <div class="row" style="margin:0"><input list="models_${name}" id="cfg_${name}_${f.key}" placeholder="${f.placeholder||''}" value="${esc(d.config[f.key]||'')}" style="flex:1">
      <datalist id="models_${name}"></datalist>
      <button class="ghost" onclick="fetchModels('${name}')" id="fetchbtn_${name}">拉取模型</button></div></div>`;
      return `<div class="field"><label>${f.label}${f.secret?'（留空=不修改）':''}</label>
      <div class="row" style="margin:0"><input ${f.secret?'type="password" autocomplete="off"':''} id="cfg_${name}_${f.key}" placeholder="${f.placeholder||''}" value="${esc(d.config[f.key]||'')}" style="flex:1"${f.secret?' data-secret="1"':''}>${f.secret?`<button class="ghost" type="button" onclick="togglePw(this)" title="显示/隐藏">👁</button>`:''}</div></div>`;}).join('');
    cards.push(`<div class="card"><h2>${d.mcp.label} <span class="badge">${d.mcp.path}</span></h2>
      <div class="sub" style="margin-bottom:8px">${d.mcp.desc}</div>
      ${name==='codex'?'<div class="row" id="codexStatus" style="margin-bottom:8px"></div>':''}${fields}
      ${d.mcp.restart_needed?`<div class="hint">注意：此 MCP 的配置需重启容器后生效（docker restart ${d.mcp.container||name+'mcp'}）</div>`:''}
      <div class="row" style="margin-top:12px"><button onclick="saveCfg('${name}')">保存</button><span id="cfgmsg_${name}" class="hint"></span></div></div>`);
  }
  $('configCards').innerHTML=cards.join('');
  if(CATALOG['codex'])renderCodexStatus();
}
async function renderCodexStatus(){
  try{
    const r=await api('/admin/api/codex/status');const d=await r.json();
    const el=$('codexStatus');if(!el)return;
    let s='';
    if(d.chatgpt_login)s+=`<span class="badge on">ChatGPT 已登录（${esc(d.chatgpt_login)}）</span> `;
    else s+=`<span class="badge off">ChatGPT 未登录</span> `;
    if(d.third_party_ready)s+=`<span class="badge on">第三方 API 就绪（${esc(d.model)} @ ${esc(d.base_url)}）</span>`;
    else s+=`<span class="badge off">第三方 API 未配置</span>`;
    el.innerHTML=s+`<div class="hint" style="margin-top:6px">ChatGPT 登录（可选，与第三方 API 二选一）：服务器执行 <code>docker exec -it mcp-codexmcp codex login</code> 按提示浏览器授权；或 API Key 方式 <code>docker exec -i mcp-codexmcp codex login --with-api-key</code>（Key 从 stdin 传入）。凭据持久化在 data/codex/home/，容器重建不丢。</div>`;
  }catch(e){}
}
async function fetchModels(name){
  const btn=$('fetchbtn_'+name);btn.disabled=true;btn.textContent='拉取中…';
  try{
    // 先保存当前输入（含未掩码的新 Key），再拉取
    const body={};
    document.querySelectorAll(`[id^="cfg_${name}_"]`).forEach(i=>body[i.id.replace(`cfg_${name}_`,'')]=i.value);
    await api(`/admin/api/config/${name}`,{method:'POST',body:JSON.stringify(body)});
    const r=await api(`/admin/api/models/${name}`,{method:'POST',body:JSON.stringify(body)});
    const d=await r.json();
    if(!r.ok){toast(d.error||'拉取失败',false);return}
    $('models_'+name).innerHTML=d.models.map(m=>`<option value="${esc(m)}">`).join('');
    toast(`已拉取 ${d.count} 个模型，点击模型输入框选择`);
  }finally{btn.disabled=false;btn.textContent='拉取模型'}
}
async function saveCfg(name){
  const body={};
  document.querySelectorAll(`[id^="cfg_${name}_"]`).forEach(i=>body[i.id.replace(`cfg_${name}_`,'')]=i.value);
  const r=await api(`/admin/api/config/${name}`,{method:'POST',body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){toast('保存失败',false);return}
  toast(d.restart_needed?'已保存，需重启容器生效':'已保存（热生效）');
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

// 初始：探测是否已设密码
(async()=>{const r=await fetch('/admin/api/tokens');if(r.status===401)showLogin();else enterMain()})();
</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(_PAGE)
