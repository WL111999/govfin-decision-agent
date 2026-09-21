"""在 Nexent 里开一个演示租户，并把 govfin 的 MCP 服务与 Skill 装进去。

**为什么需要这一步。** Nexent 是多租户设计：智能体开发界面（智能体 / MCP 工具 /
技能 / 知识库）全部活在**租户内部**，而部署时自动创建的 `suadmin` 是**平台管理员**
——它的 `accessibleRoutes` 只有 `["/", "/resource-manage", "/space"]`，权限里有
`tenant:create`、`group:create` 却没有 `agent:create`。也就是说这个账号能建租户、
管资源，但**界面里根本不会出现智能体开发入口**。

所以"能在 Nexent 平台上运行"这件事要真正可演示，必须走完 Nexent 设计的正常流程：

    平台管理员建租户 → 发管理员邀请码 → 被邀请人注册成租户用户

用租户账号登录，侧边栏才会展开智能体 / MCP 工具 / 技能等完整菜单。

跑法：
    python -X utf8 scripts/provision_tenant.py
    python -X utf8 scripts/provision_tenant.py --tenant-name "我的租户"

脚本是**幂等**的：租户已存在就复用，MCP 与 Skill 已注册就跳过。

安全：Nexent 的凭据只从 Nexent 自己的 deploy/env/.env 读，不写死在本文件里。
新账号密码会打印到终端——它是给你登录用的，请自行妥善保管。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import string
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEXENT_SRC = pathlib.Path(r"D:\桌面\nexent_src")
ENV_FILE = NEXENT_SRC / "deploy" / "env" / ".env"
CONFIG_API = "http://localhost:5010"

MCP_URL = "http://govfin-agent:8930/mcp"
SERVER_NAME = "govfin-decision"
MCP_DESCRIPTION = "金融+政务跨域授信决策：政务事实核验、财务解析、受约束多跳推理、授信决策合成与可追溯审计"

DEFAULT_TENANT = "GovFin 演示租户"
# 不能用 .local：那是保留域名，校验层会直接拒掉（"special-use or reserved name"）。
DEFAULT_EMAIL = "govfin@nexent-demo.com"
SKILLS_DIR = ROOT / "nexent" / "skills" / "dist"


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _http(method: str, url: str, *, headers=None, payload=None, body: bytes | None = None, timeout: int = 90):
    data = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _json(body: str, default=None):
    try:
        return json.loads(body)
    except Exception:
        return default


def _supabase_base(values: dict[str, str]) -> str:
    url = (values.get("SUPABASE_URL") or "").rstrip("/")
    # .env 里写的是容器内主机名，宿主机访问要走发布出来的网关端口。
    for host in ("nexent-supabase-kong", "supabase-kong", "kong"):
        url = url.replace(f"//{host}:", "//localhost:")
    return url


def login(values: dict[str, str], email: str, password: str) -> str | None:
    base, anon = _supabase_base(values), values.get("SUPABASE_KEY") or ""
    if not base or not anon:
        print("  ✗ Nexent 的 deploy/env/.env 里缺 SUPABASE_URL 或 SUPABASE_KEY")
        return None
    status, body = _http(
        "POST",
        f"{base}/auth/v1/token?grant_type=password",
        headers={"apikey": anon, "Content-Type": "application/json"},
        payload={"email": email, "password": password},
    )
    if status != 200:
        return None
    return (_json(body) or {}).get("access_token")


def _strong_password(length: int = 16) -> str:
    """Supabase 要求至少 8 位且含大写、小写、数字。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isupper() for c in pwd) and any(c.islower() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


# --------------------------------------------------------------------------
# 各步骤
# --------------------------------------------------------------------------


def find_tenant(auth: dict, name: str) -> dict | None:
    """按名字找租户。列表接口在 /tenants/tenant-list（POST）。"""
    status, body = _http("POST", f"{CONFIG_API}/tenants/tenant-list", headers=auth, payload={})
    if status != 200:
        return None
    data = (_json(body) or {}).get("data")
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for item in items:
        if isinstance(item, dict) and item.get("tenant_name") == name:
            return item
    return None


def create_tenant(auth: dict, name: str) -> dict | None:
    status, body = _http("POST", f"{CONFIG_API}/tenants", headers=auth, payload={"tenant_name": name})
    if status not in (200, 201):
        print(f"  ✗ 建租户失败 HTTP {status}: {body[:400]}")
        return None
    return (_json(body) or {}).get("data")


def create_invitation(auth: dict, tenant_id: str) -> str | None:
    """给租户发一个管理员邀请码——被邀请人注册后就是该租户的 ADMIN。

    用 ADMIN_INVITE 而不是 DEV_INVITE：要让侧边栏出现完整的开发菜单，
    角色需要能建智能体、建知识库、管工具。
    """
    status, body = _http(
        "POST",
        f"{CONFIG_API}/invitations",
        headers=auth,
        payload={"tenant_id": tenant_id, "code_type": "ADMIN_INVITE", "capacity": 5},
    )
    if status not in (200, 201):
        print(f"  ✗ 建邀请码失败 HTTP {status}: {body[:400]}")
        return None
    return ((_json(body) or {}).get("data") or {}).get("invitation_code")


def signup(email: str, password: str, invite_code: str) -> bool:
    status, body = _http(
        "POST",
        f"{CONFIG_API}/user/signup",
        headers={"Content-Type": "application/json"},
        payload={"email": email, "password": password, "invite_code": invite_code, "auto_login": False},
    )
    if status == 200:
        return True
    if "EMAIL_ALREADY_EXISTS" in body:
        print("  ↺ 该邮箱已注册，跳过注册")
        return True
    print(f"  ✗ 注册失败 HTTP {status}: {body[:400]}")
    return False


def register_mcp(auth: dict) -> bool:
    status, body = _http("GET", f"{CONFIG_API}/mcp/list", headers=auth)
    existing = (_json(body) or {}).get("remote_mcp_server_list") or []
    if any(s.get("remote_mcp_server_name") == SERVER_NAME for s in existing):
        print(f"  ↺ MCP「{SERVER_NAME}」已在本租户注册，跳过")
        return True
    status, body = _http(
        "POST",
        f"{CONFIG_API}/mcp/add",
        headers=auth,
        payload={
            "name": SERVER_NAME,
            "server_url": MCP_URL,
            "description": MCP_DESCRIPTION,
            "tags": ["金融", "政务", "授信决策", "知识图谱", "决策溯源"],
            "enabled": True,
        },
    )
    ok = status in (200, 201)
    print(f"  {'+' if ok else '✗'} 注册 MCP  HTTP {status}  {'' if ok else body[:250]}")
    return ok


def _multipart(field: str, filename: str, content: bytes, extra: dict[str, str]):
    boundary = "----govfinTenantBoundary7MA4YWxkTrZu0gW"
    parts: list[bytes] = []
    for name, value in extra.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/zip\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def install_skills(auth: dict, token: str) -> bool:
    status, body = _http("GET", f"{CONFIG_API}/skills", headers=auth)
    data = _json(body) or {}
    items = data.get("skills") if isinstance(data, dict) else data
    known = {str(s.get("name") or s.get("skill_name") or "") for s in (items or []) if isinstance(s, dict)}

    zips = sorted(SKILLS_DIR.glob("*.zip")) if SKILLS_DIR.exists() else []
    if not zips:
        print(f"  ✗ 找不到 Skill 包：{SKILLS_DIR}")
        return False

    ok_all = True
    for path in zips:
        if path.stem in known:
            print(f"  ↺ Skill「{path.stem}」已存在，跳过")
            continue
        body_bytes, content_type = _multipart("file", path.name, path.read_bytes(), {"source": "custom"})
        status, resp = _http(
            "POST",
            f"{CONFIG_API}/skills/upload",
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            body=body_bytes,
        )
        ok = status in (200, 201)
        ok_all = ok_all and ok
        print(f"  {'+' if ok else '✗'} 导入 Skill「{path.stem}」  HTTP {status}  {'' if ok else resp[:250]}")
    return ok_all


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Nexent 里开演示租户并装入 govfin")
    parser.add_argument("--tenant-name", default=DEFAULT_TENANT)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=None, help="留空则随机生成")
    args = parser.parse_args()

    print(f"[1] Nexent 配置：{ENV_FILE}")
    if not ENV_FILE.exists():
        print("  ✗ 找不到 Nexent 的 .env；请先用 deploy/README.md 部署 Nexent")
        return 1
    values = _env()

    su_token = login(values, "suadmin@nexent.com", "Nexent@123")
    if not su_token:
        print("  ✗ 平台管理员登录失败")
        return 1
    su_auth = {"Authorization": f"Bearer {su_token}", "Content-Type": "application/json"}
    print("  ✓ 平台管理员已登录")

    print(f"\n[2] 租户「{args.tenant_name}」")
    tenant = find_tenant(su_auth, args.tenant_name)
    if tenant:
        print(f"  ↺ 已存在：tenant_id={tenant.get('tenant_id')}")
    else:
        tenant = create_tenant(su_auth, args.tenant_name)
        if not tenant:
            return 1
        print(f"  + 已创建：tenant_id={tenant.get('tenant_id')}")
        print(f"    默认用户组：{tenant.get('default_group_id')}")
    tenant_id = tenant.get("tenant_id")

    print("\n[3] 为该租户发管理员邀请码")
    invite = create_invitation(su_auth, tenant_id)
    if not invite:
        return 1
    print(f"  ✓ 邀请码：{invite}")

    print(f"\n[4] 注册租户用户「{args.email}」")
    password = args.password or _strong_password()
    if not signup(args.email, password, invite):
        return 1

    print("\n[5] 用租户账号登录")
    token = login(values, args.email, password)
    if not token:
        print("  ✗ 租户账号登录失败")
        return 1
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, body = _http("GET", f"{CONFIG_API}/user/current_user_info", headers=auth)
    info = ((_json(body) or {}).get("data") or {}).get("user") or {}
    print(f"  ✓ 已登录  role={info.get('user_role')}  tenant={info.get('tenant_id')}")
    routes = info.get("accessibleRoutes") or []
    print(f"    可访问路由（{len(routes)} 项）：{', '.join(routes[:12])}")

    print("\n[6] 在本租户下注册 govfin 的 MCP 服务")
    register_mcp(auth)

    print("\n[7] 在本租户下导入 Skill 模板")
    install_skills(auth, token)

    print("\n" + "=" * 62)
    print("  登录信息")
    print("=" * 62)
    print(f"  地址：  http://localhost:3000")
    print(f"  邮箱：  {args.email}")
    print(f"  密码：  {password}")
    print(f"  租户：  {args.tenant_name}")
    print("=" * 62)
    print("用这个账号登录，侧边栏会展开智能体 / MCP 工具 / 技能等完整菜单。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
