r"""把领域文档自动灌进 Nexent 知识库。

**为什么单独做这一步。** 智能体回答"连续三个月社保异常要怎么办"时，图里只有结构化
事实（哪家企业、哪个月欠缴），没有**制度依据**；政策条款躺在文本文件里，不在图上。
知识库补的就是这一段：结构化事实回答"发生了什么"，知识库回答"按规定该怎么办"。

跑法：
    python -X utf8 scripts/sync_knowledge_base.py
    python -X utf8 scripts/sync_knowledge_base.py --dry-run    # 只列出要传什么
    python -X utf8 scripts/sync_knowledge_base.py --force      # 重传（默认跳过已存在的）

脚本是**幂等**的：已经传过的文件按文件名跳过。幂等很重要——这个脚本会被一键部署
反复调用，每次全量重传的话，重复文档会在检索结果里挤掉真正相关的那几条。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEXENT_SRC = pathlib.Path(r"nexent_src")
ENV_FILE = NEXENT_SRC / "deploy" / "env" / ".env"
CONFIG_API = "http://localhost:5010"

KB_NAME = "govfin-regulation"
KB_DESCRIPTION = "金融+政务领域制度依据：监管条款、征信规范、司法文书"

# 进知识库的是**制度性与叙述性**材料。结构化的事实数据（社保记录、工商登记的
# JSON）刻意不放——那些应该走图谱查询，放进知识库只会让检索结果里混进一堆字段
# 片段，反而干扰对条款的召回。
SOURCES = (
    "data/fin/监管条款汇编.txt",
    "data/fin/甲科技_征信报告.txt",
    "data/fin/甲科技_2025年度财务报表.pdf",
    "data/gov/民事判决书_SF-2026-01-0233.pdf",
)


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


def _http(method: str, url: str, *, headers=None, payload=None, body: bytes | None = None, timeout: int = 120):
    data = body if body is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _json(raw: str, default=None):
    try:
        return json.loads(raw)
    except Exception:
        return default


def login(values: dict[str, str], email: str, password: str) -> str | None:
    url = (values.get("SUPABASE_URL") or "").rstrip("/")
    for host in ("nexent-supabase-kong", "supabase-kong", "kong"):
        url = url.replace(f"//{host}:", "//localhost:")
    status, body = _http(
        "POST", f"{url}/auth/v1/token?grant_type=password",
        headers={"apikey": values.get("SUPABASE_KEY", ""), "Content-Type": "application/json"},
        payload={"email": email, "password": password},
    )
    if status != 200:
        return None
    return (_json(body, {}) or {}).get("access_token")


def _multipart(files: list[tuple[str, str, bytes]], fields: dict[str, str]):
    boundary = "----govfinKbBoundary7MA4YWxkTrZu0gW"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for field, filename, content in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def find_kb(auth: dict, name: str) -> dict | None:
    """按知识库**名字**找到它，并取回真正的 index_name。

    这里有个容易踩的坑：知识库名字和索引名是**两个东西**。创建时给它起的
    ``govfin-regulation`` 只是 display_name，实际的 index_name 是后端生成的
    ``1-b32ee36b...``。而上传文档、查文件列表用的都是后者。

    ``GET /indices`` 默认只回一串索引 ID，看不出哪个是我们的；加上
    ``include_stats=true`` 才会带上 ``display_name`` 和 ``name``（= index_name）。
    不加这个参数的话，代码会以为"知识库不存在"，然后去重复创建，拿到 409。
    """
    status, body = _http("GET", f"{CONFIG_API}/indices?include_stats=true", headers=auth)
    if status != 200:
        return None
    data = _json(body, {}) or {}
    for item in data.get("indices_info") or []:
        if isinstance(item, dict) and item.get("display_name") == name:
            return item
    return None


def create_kb(auth: dict, name: str, embedding_model_id: int) -> dict | None:
    status, body = _http(
        "POST", f"{CONFIG_API}/indices/{name}", headers=auth,
        payload={"embedding_model_id": embedding_model_id, "ingroup_permission": "EDIT",
                 "preserve_source_file": True},
    )
    if status not in (200, 201):
        print(f"  建知识库失败 HTTP {status}: {body[:300]}")
        return None
    return _json(body, {})


def _base_name(filename: str) -> str:
    """去掉 Nexent 为同名文件自动加的 ``_N`` 后缀。

    重名时它把 ``监管条款汇编.txt`` 存成 ``监管条款汇编_2.txt``。不归一化的话，
    幂等判断会把"已经传过了"看成"从没传过"，于是每跑一次就多一份——
    而知识库里塞满重复文档的后果不是报错，是检索结果被同一段话的不同副本挤满，
    真正相关的那几条反而排不进 top_k。
    """
    stem = pathlib.Path(filename).stem
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        stem = parts[0]
    return stem + pathlib.Path(filename).suffix


def existing_filenames(auth: dict, index_name: str) -> set[str]:
    """已经进库的文件名（已归一化）。用于幂等跳过。"""
    status, body = _http("GET", f"{CONFIG_API}/indices/{index_name}/files", headers=auth)
    if status != 200:
        return set()
    data = _json(body, {})
    items = data.get("files") if isinstance(data, dict) else data
    names: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            for key in ("file_name", "file", "filename", "name", "path_or_url"):
                if item.get(key):
                    names.add(_base_name(str(item[key])))
        elif isinstance(item, str):
            names.add(_base_name(item))
    return names


def upload_files(auth: dict, token: str, index_name: str, paths: list[pathlib.Path]) -> bool:
    """上传 + 触发切块向量化。

    两步是 Nexent 的设计：先落对象存储，再由 data-process 服务异步切块入库。
    合并成一步看起来更省事，但那样上传请求会被切块耗时拖住，大文件直接超时。
    """
    files = [("file", p.name, p.read_bytes()) for p in paths]
    body, content_type = _multipart(files, {"index_name": index_name, "destination": "minio",
                                            "folder": "knowledge_base"})
    status, resp = _http(
        "POST", f"{CONFIG_API}/file/upload",
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                 "User-Agent": "AgentFrontEnd/1.0"},
        body=body, timeout=180,
    )
    if status not in (200, 201):
        print(f"  上传失败 HTTP {status}: {resp[:300]}")
        return False
    result = _json(resp, {}) or {}
    uploaded = result.get("uploaded_file_paths") or []
    filenames = result.get("uploaded_filenames") or []
    if not uploaded:
        print(f"  上传返回里没有文件路径: {resp[:250]}")
        return False
    print(f"  已上传 {len(uploaded)} 个文件")

    records = result.get("file_records") or []
    to_process = [
        {"path_or_url": path, "filename": filenames[i] if i < len(filenames) else pathlib.Path(path).name,
         "file_id": next((r.get("file_id") for r in records if r.get("object_name") == path), None)}
        for i, path in enumerate(uploaded)
    ]
    # /file/process 收的是三个**平级字段**（files / index_name / destination），
    # 不是把文件数组直接当 body。只传数组会拿到 422，报错列出四个"字段缺失"，
    # 看起来像是漏传了很多东西，其实只是包错了一层。
    status, resp = _http("POST", f"{CONFIG_API}/file/process",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         payload={"files": to_process, "index_name": index_name, "destination": "minio",
                                  "chunking_strategy": "basic"},
                         timeout=180)
    if status not in (200, 201):
        print(f"  触发切块失败 HTTP {status}: {resp[:300]}")
        return False
    print("  已触发切块与向量化（异步，稍等片刻生效）")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="把领域文档灌进 Nexent 知识库")
    parser.add_argument("--email", default="govfin@nexent-demo.com")
    parser.add_argument("--password", default="***REDACTED***")
    parser.add_argument("--kb-name", default=KB_NAME)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="重传已存在的文件")
    args = parser.parse_args()

    print(f"[1] Nexent 配置：{ENV_FILE}")
    if not ENV_FILE.exists():
        print("  ✗ 找不到 Nexent 的 .env；请先部署 Nexent")
        return 1
    values = _env()
    token = login(values, args.email, args.password)
    if not token:
        print("  ✗ 登录失败；请确认租户账号密码（见 provision_tenant.py 的输出）")
        return 1
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"  ✓ 已登录 {args.email}")

    print(f"\n[2] 知识库「{args.kb_name}」")
    kb = find_kb(auth, args.kb_name)
    if kb:
        index_name = kb.get("name") or kb.get("index_name")
        print(f"  ↺ 已存在：{args.kb_name}（索引 {index_name}）")
    else:
        status, body = _http("GET", f"{CONFIG_API}/model/list?model_type=embedding", headers=auth)
        models = (_json(body, {}) or {}).get("data") or []
        if not models:
            print("  ✗ 没有 embedding 模型。先跑一键部署脚本，或到 Nexent 模型配置页添加")
            return 1
        model_id = models[0].get("model_id")
        print(f"  + 用向量模型「{models[0].get('display_name')}」(id={model_id}) 创建")
        created = create_kb(auth, args.kb_name, model_id)
        if not created:
            return 1
        index_name = created.get("id") or created.get("name") or args.kb_name
        print(f"    创建成功：{index_name}")
        print(f"    描述：{KB_DESCRIPTION}")

    print("\n[3] 待导入文档")
    paths = [ROOT / rel for rel in SOURCES]
    missing = [p for p in paths if not p.exists()]
    paths = [p for p in paths if p.exists()]
    if missing:
        for p in missing:
            print(f"  ! 找不到 {p.relative_to(ROOT)}")
    if not paths:
        print("  ✗ 没有可导入的文档")
        return 1

    known = set() if args.force else existing_filenames(auth, index_name)
    todo = [p for p in paths if p.name not in known]
    for p in paths:
        mark = "跳过（已存在）" if p.name in known else "待导入"
        print(f"  - {p.name}  ({p.stat().st_size // 1024} KB)  {mark}")

    if args.dry_run:
        print("\n--dry-run：未做任何改动")
        return 0
    if not todo:
        print("\n全部已存在，无需导入。要强制重传加 --force")
        return 0

    print(f"\n[4] 导入 {len(todo)} 个文档")
    ok = upload_files(auth, token, index_name, todo)
    print("\n完成。" if ok else "\n导入未成功，请检查上面的报错。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
