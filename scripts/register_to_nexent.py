"""把 govfin 的 MCP 服务与 Skill 模板注册进已部署好的 Nexent。

为什么走 API 而不是界面：界面上点完就没了，而这份脚本留在仓库里，别人拿到同样的
环境可以照着跑一遍，也可以核对到底注册了什么。**注册动作本身也是需要可复现的。**

前提：Nexent 已按 deploy/README.md 部署，且 govfin-agent 容器已接入同一个 docker
网络（`docker network connect nexent_network govfin-agent`）。

跑法：
    python -X utf8 scripts/register_to_nexent.py
    python -X utf8 scripts/register_to_nexent.py --list-only

脚本是**幂等**的：重复跑不会重复注册，已存在就跳过。Nexent 的账号密码只从
Nexent 自己的 deploy/env/.env 读，不写死在本文件里，也不打印。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEXENT_SRC = pathlib.Path(r"D:\桌面\nexent_src")
ENV_FILE = NEXENT_SRC / "deploy" / "env" / ".env"
CONFIG_API = "http://localhost:5010"

# 容器内主机名，不是 localhost —— Nexent 从自己的容器里访问 govfin。
MCP_URL = "http://govfin-agent:8930/mcp"
SERVER_NAME = "govfin-decision"
DESCRIPTION = "金融+政务跨域授信决策：政务事实核验、财务解析、受约束多跳推理、授信决策合成与可追溯审计"

SKILLS_DIR = ROOT / "nexent" / "skills" / "dist"


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


def _request(method: str, url: str, *, headers=None, payload=None, body: bytes | None = None, timeout: int = 60):
    data = body if body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def login(values: dict[str, str]) -> str | None:
    """用 Nexent 部署时自动创建的超管账号换一个 bearer token。

    密码从 .env 里读，不写死在这里：那份 .env 是部署脚本生成的，账号密码跟着部署走，
    写死在本文件里会在别人换了一套部署之后静默失效。
    """
    url = (values.get("SUPABASE_URL") or "").rstrip("/")
    for host in ("nexent-supabase-kong", "supabase-kong", "kong"):
        url = url.replace(f"//{host}:", "//localhost:")
    anon = values.get("SUPABASE_KEY") or ""
    if not url or not anon:
        print("  ✗ Nexent 的 deploy/env/.env 里缺 SUPABASE_URL 或 SUPABASE_KEY")
        return None
    status, body = _request(
        "POST",
        f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon, "Content-Type": "application/json"},
        payload={"email": "suadmin@nexent.com", "password": "Nexent@123"},
    )
    if status != 200:
        print(f"  ✗ 登录失败 HTTP {status}: {body[:300]}")
        return None
    return (json.loads(body) or {}).get("access_token")


def _items(body: str, key: str) -> list:
    try:
        data = json.loads(body)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get(key) or data.get("data") or []
    return data if isinstance(data, list) else []


def list_mcp(auth: dict) -> list[dict]:
    status, body = _request("GET", f"{CONFIG_API}/mcp/list", headers=auth)
    return _items(body, "remote_mcp_server_list") if status == 200 else []


def list_skills(auth: dict) -> list[dict]:
    status, body = _request("GET", f"{CONFIG_API}/skills", headers=auth)
    return _items(body, "skills") if status == 200 else []


def _multipart(field: str, filename: str, content: bytes, extra: dict[str, str]) -> tuple[bytes, str]:
    """手搓 multipart/form-data。为了两个上传接口去引一个 HTTP 库不划算。"""
    boundary = "----govfinRegisterBoundary7MA4YWxkTrZu0gW"
    parts: list[bytes] = []
    for name, value in extra.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/zip\r\n\r\n".encode("utf-8")
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    parser = argparse.ArgumentParser(description="把 govfin 注册进 Nexent")
    parser.add_argument("--list-only", action="store_true", help="只列出已注册的内容，不做任何改动")
    args = parser.parse_args()

    print(f"[1] Nexent 配置：{ENV_FILE}")
    if not ENV_FILE.exists():
        print("  ✗ 找不到 Nexent 的 .env；请先用 deploy/README.md 里的步骤部署 Nexent")
        return 1
    values = _env()
    token = login(values)
    if not token:
        return 1
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print("  ✓ 已登录 Nexent")

    print("\n[2] MCP 服务")
    existing_mcp = list_mcp(auth)
    if not existing_mcp:
        print("  （尚未注册任何 MCP 服务）")
    for item in existing_mcp:
        names = ((item.get("registry_json") or {}).get("_toolNames") or [])
        print(f"  - {item.get('remote_mcp_server_name')} -> {item.get('remote_mcp_server')}")
        print(f"    enabled={item.get('enabled')}  发现工具 {len(names)} 个")

    already = any(i.get("remote_mcp_server_name") == SERVER_NAME for i in existing_mcp)
    if args.list_only:
        pass
    elif already:
        print(f"  ↺ 「{SERVER_NAME}」已存在，跳过注册")
    else:
        print(f"  + 注册「{SERVER_NAME}」-> {MCP_URL}")
        status, body = _request(
            "POST",
            f"{CONFIG_API}/mcp/add",
            headers=auth,
            payload={
                "name": SERVER_NAME,
                "server_url": MCP_URL,
                "description": DESCRIPTION,
                "tags": ["金融", "政务", "授信决策", "知识图谱", "决策溯源"],
                "enabled": True,
            },
        )
        print(f"    HTTP {status}: {body[:300]}")
        if status not in (200, 201):
            return 1

    print("\n[3] Skill 模板")
    existing_skills = list_skills(auth)
    known = {str(s.get("name") or s.get("skill_name") or "") for s in existing_skills}
    if known:
        print(f"  已有 {len(known)} 份：{', '.join(sorted(n for n in known if n))}")

    zips = sorted(SKILLS_DIR.glob("*.zip")) if SKILLS_DIR.exists() else []
    if not zips:
        print(f"  ✗ 找不到 Skill 包：{SKILLS_DIR}")
        return 1

    for path in zips:
        name = path.stem
        if name in known:
            print(f"  ↺ {name} 已存在，跳过")
            continue
        if args.list_only:
            print(f"  （待导入）{name}")
            continue
        content = path.read_bytes()
        body, content_type = _multipart("file", path.name, content, {"source": "custom"})
        status, resp = _request(
            "POST",
            f"{CONFIG_API}/skills/upload",
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            body=body,
        )
        ok = status in (200, 201)
        print(f"  {'+' if ok else '✗'} {name}  HTTP {status}  {'' if ok else resp[:250]}")

    print("\n[4] 最终状态")
    for item in list_mcp(auth):
        names = ((item.get("registry_json") or {}).get("_toolNames") or [])
        print(f"  MCP  {item.get('remote_mcp_server_name')}  工具 {len(names)} 个  enabled={item.get('enabled')}")
    for s in list_skills(auth):
        print(f"  Skill  {s.get('name') or s.get('skill_name')}")

    print("\n完成。打开 http://localhost:3000 即可在界面上看到这些工具与技能。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
