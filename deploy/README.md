# 部署与 Nexent 集成

本目录是 govfin-decision-agent 接入华为 ModelEngine **Nexent** 平台的全部材料。

- [10 秒验证](#10-秒验证) — 不装 Docker 先跑通
- [方式一：独立部署](#方式一独立部署) — 起 MCP + A2A 服务，任意 MCP 客户端可接
- [方式二：接入 Nexent](#方式二接入-nexent) — 完整部署 Nexent 并注册本智能体
- [Skill 注册](#skill-注册) — 把三个工作流模板挂到 Nexent
- [环境变量](#环境变量)
- [故障排查](#故障排查)

---

## 10 秒验证

不装 Docker、不配 key，直接在本机跑通完整决策链路：

```bash
cd govfin-decision-agent
export PYTHONPATH=src                       # Windows: set PYTHONPATH=src
python -X utf8 -m govfin.cli --db data/govfin_graph.db ingest --data data  # 导入样例文档
python -X utf8 -m govfin.cli --db data/govfin_graph.db doctor              # 自检
python -X utf8 -m govfin.cli --db data/govfin_graph.db ask 甲科技有限公司 --brief
```

三条命令必须用**同一个 `--db`**。用一个库导入、换一个库自检，自检看到的是一张空图——
而空图与"数据没导进去"在自检报告里长得一模一样。也不要在这里用 `--in-memory`：
内存图随进程退出即消失，下一条命令拿不到上一条的成果。

`doctor` 是部署前最该跑的一条命令。它检查的四类问题在运行期**全是静默的**——
图谱为空只会让结论变空，不会抛异常；本体版本不匹配会让推理走到不存在的类上而不报错。
这些在日志里看不出来，只能主动查。全部通过时长这样：

```
图谱非空        33 节点 / 55 边
实体图有内容    23 节点 / 26 边
规则图有内容    10 节点 / 29 边
运行时图        尚无决策痕迹（首次决策后填充，属正常状态）
决策通路        甲科技有限公司 → 审慎核定（置信度 0.5803，10 条采纳路径 / 1 条阈值判定）
图完整性        无异常
MCP 工具注册    10 个
LLM 通道        deepseek/deepseek-chat 已配置
```

---

## 方式一：独立部署

```bash
cd deploy
cp .env.example .env          # 全部留空也能跑，只是本体演化走纯符号路径
docker compose up -d
curl http://localhost:8940/health
```

起来之后：

| 端点 | 用途 |
|---|---|
| `http://localhost:8930/mcp` | MCP 工具服务（streamable-http），10 个领域工具 |
| `http://localhost:8940/.well-known/agent-card.json` | A2A Agent Card，客户端靠它发现能力 |
| `http://localhost:8940/agents` | 完整协作拓扑：1 个 orchestrator + 6 个 worker |
| `http://localhost:8940/health` | 健康检查，含图谱规模与本体版本 |

验证 MCP 工具能否被调用：

```bash
python - <<'PY'
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:8930/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("工具数:", len(tools.tools))
            out = await s.call_tool("graph_stats", {})
            print(json.dumps(out.structuredContent, ensure_ascii=False, indent=2))

asyncio.run(main())
PY
```

---

## 方式二：接入 Nexent

### 1. 部署 Nexent

要求 Docker 24+ 与 Docker Compose v2+。

```bash
git clone https://github.com/ModelEngine-Group/nexent.git
cd nexent
bash deploy.sh docker
```

交互式 TUI 里 **`infrastructure` 是必选项**（含 Elasticsearch、PostgreSQL、Redis、MinIO），
其余三个组件默认开启。配置写在 `deploy/env/.env`。

国内网络建议加 `--image-source mainland` 走国内镜像源。

首次启动较慢：基础设施组件要拉镜像并初始化，Elasticsearch 初始化通常需要 1–3 分钟。
等到 Web 界面可访问再继续。

### 2. 让两个容器互相看得见

`govfin-agent` 必须和 Nexent 处在**同一个 docker 网络**里，否则 Nexent 只能用
宿主机 IP 访问，而容器内的 `localhost` 指向的是它自己。

把本仓库的 `deploy/docker-compose.yml` 里的 `govfin-agent` 服务段整体复制到
Nexent 的 `deploy/docker/docker-compose.yml` 的 `services:` 下，并做两处改动：

```yaml
services:
  govfin-agent:
    # ... 保持原样 ...

    networks:
      - nexent-net        # ← 改成 Nexent 的网络名（用 docker network ls 确认）
```

同时把 `volumes` 段的相对路径 `../data` 改成绝对路径或去掉——拼接后的相对路径
会指向 Nexent 的目录结构。

改完重启：

```bash
cd nexent
docker compose -f deploy/docker/docker-compose.yml up -d
docker exec nexent-nexent-1 curl -fsS http://govfin-agent:8930/mcp   # 连通性自测
```

最后一条命令是关键。如果这里不通，无论 Nexent 界面里怎么配都不会通。

### 3. 注册 MCP 服务

#### 路径零：跑脚本（推荐）

```bash
cd govfin-decision-agent
python -X utf8 scripts/register_to_nexent.py
```

这一步会**同时**完成 MCP 服务注册与 3 份 Skill 导入，幂等、可重复跑、可核对。
相比在界面上点，它的好处是注册动作本身也变得可复现——换一台机器不用回忆当时填了什么。

脚本跑完会打印 Nexent 实际**发现到**的工具清单。那份清单是 Nexent 自己连上 govfin
的 MCP 端点后拿到的，不是我们写进去的声明，所以它同时证明了网络通、协议握手成功、
工具清单能正确解析这三件事。

想手动做，或者要核对脚本做了什么，再用下面两条界面路径。

#### 路径一：按 URL 添加

MCP 配置页 → 添加服务，填两个字段：

| 字段 | 值 |
|---|---|
| 服务名 | `govfin-decision` |
| 服务 URL | `http://govfin-agent:8930/mcp` |

> **注意 URL 里用的是容器名 `govfin-agent`，不是 `localhost`。**
> 这是接入失败最常见的原因：在宿主机浏览器里能打开的地址，
> 在 Nexent 容器里指向的是它自己。

#### 路径二：导入 [`mcp_config.json`](mcp_config.json)

MCP 配置页 → 导入配置，粘贴本文件内容，另需在界面上填容器端口（`8930`）与服务名。

这条路径下 Nexent 会用配置里的 `image` 起一个**代理容器**来托管该 MCP 服务。
因此配置里**必须有 `command` 与 `args`** ——Nexent 的 `MCPServerConfig`
（`backend/consts/model.py`）把 `command` 定义为必填项，
只给 URL 会通不过校验。本文件已按该 schema 写好。

两条路径二选一即可，不要同时加，否则工具列表里会出现重复条目。**已经跑过路径零的，
这两条都不用做了**——脚本注册的服务与它们完全等价。

注册成功后 MCP 工具列表里应出现 10 个 `govfin` 工具（见 `mcp_config.json`
的 `expectedTools`）。若只出现一部分，说明某个工具在注册时抛了异常——
看 govfin 容器日志，工具注册失败不会影响其他工具注册。

### 4. 创建智能体并挂载 Skill

在 Nexent 里新建智能体，工具选中全部 10 个 `govfin` 工具，
再按 [Skill 注册](#skill-注册) 挂上三个工作流模板。

建议的系统提示词：

```
你是金融+政务跨域授信决策智能体。严格按以下顺序工作：

1. 任何授信问题，第一步必须是 gov_business_lookup 核验主体真实性。
   未通过核验时立即终止，不产生任何授信结论。
2. 核验通过后，用 gov_social_security / gov_judicial_scan / fin_financial_parser
   采集事实，用 kg_path_query 做关联方风险传导分析。
3. 用 risk_decision 合成结论，用 evidence_bundle 取回完整依据链。

硬性约束：
- found=false（查无此主体）与 ok=false（查询失败）必须区别对待。
  前者终止流程，后者重试或降级，绝不能混为一谈。
- 结论必须能追溯到条款编号。追溯不到的结论不要输出。
- 结论为"证据不足"时如实上报，不要用"未发现风险"掩盖数据缺失。
- 引用数据时必须带 source_document。
```

---

## Skill 注册

三个工作流模板在 [`../nexent/skills/`](../nexent/skills/)，是完整的、可直接部署的
编排方案，不是函数说明：

| Skill | 解决的问题 | 对应 MCP 工具 |
|---|---|---|
| [`enterprise-authenticity-check`](../nexent/skills/enterprise-authenticity-check/SKILL.md) | 主体是否真实存在（前置闸门） | `gov_business_lookup` |
| [`counterparty-risk-contagion-scan`](../nexent/skills/counterparty-risk-contagion-scan/SKILL.md) | 关联方风险是否传导到目标主体 | `kg_path_query`、`gov_*` |
| [`credit-decision-chain`](../nexent/skills/credit-decision-chain/SKILL.md) | 风险是否触碰监管阈值，结论是什么 | `risk_decision`、`evidence_bundle` |

每个 Skill 都写了**失败模式与降级策略**——这是它们区别于普通提示词模板的地方。
每个失败模式都对应一个具体的、在上游系统里真实会发生的故障，
以及该故障下应当怎么做（重试 / 降级 / 转人工 / 终止），以及**不该怎么做**。

### 挂载方式

Skill 空间 → 导入，上传 [`../nexent/skills/dist/`](../nexent/skills/dist/) 下的三个 zip：

```
enterprise-authenticity-check.zip
counterparty-risk-contagion-scan.zip
credit-decision-chain.zip
```

zip 结构与 Nexent 官方 skill 包（`deploy/docker/assets/official-skills-zip/`）一致——
顶层是 skill 名目录，内含 `SKILL.md` 与 `examples.md`，可直接导入。

三个 SKILL.md 已用 Nexent 的 `SkillLoader`（`sdk/nexent/skills/skill_loader.py`）
验证过，`name` / `description` / `allowed-tools` / `tags` 四个字段均能正确解析，
中文编码走的也是它自己的 `decode_skill_text`，无乱码。

`examples.md` 走的是渐进式披露：主文件只写常规路径，示例与边界情形放在
reference 文件里，**只在默认用法不够用时才加载**。三份主文件的正文加起来约
9,000 字，而示例文件另有约 15KB——把示例全部内联会让每次 Skill 触发都
多烧掉一倍多的上下文。

### `allowed-tools` 的取值

每个 Skill 的 `allowed-tools` 里写的是 MCP 工具名，必须与注册到 Nexent 的
工具名完全一致。若接入后发现 Skill 说"可以用 gov_business_lookup"但模型调不到，
先检查这一步——工具名对不上时不会报错，只会表现为模型不用这个工具。

三个 Skill 之间有明确的上游契约：前一个的输出是后一个的**前置条件**，
且核验不通过时后续 Skill 不应被触发。

---

## 环境变量

完整列表见 [`.env.example`](.env.example)。几个需要特别说明的：

| 变量 | 默认 | 为什么这么默认 |
|---|---|---|
| `GOVFIN_LLM_API_KEY` | 空 | **留空即离线模式**。决策与推理完全不依赖 LLM，只有本体演化的语义补全需要它。没 key 时演化退回纯符号路径，仍能学别名、做聚类对齐 |
| `GOVFIN_DECISION_THRESHOLD` | 0.55 | 全局兜底阈值。各路径约束还有自己的 `accept_threshold`，多跳链以各自的为准——用单一全局阈值会让跳数变成实际的判定者 |
| `GOVFIN_HOP_DECAY` | 0.95 | 每跳的非证据性语义漂移损耗 |
| `GOVFIN_BOTTLENECK` | 0.25 | 瓶颈保护下限：最弱环节对整体的压制程度 |
| `GOVFIN_LAMBDA_LLM` | 2.6 | LLM 生成边的惩罚系数。是 derived（1.6）的 1.6 倍、direct（1.0）的 2.6 倍 |

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| Nexent 里 MCP 连接失败 | 用了 `localhost` 而不是容器名 | 改成 `http://govfin-agent:8930/mcp`，先在 Nexent 容器内 `curl` 验证 |
| 结论恒为"证据不足" | 图是空的 | `govfin --db <path> doctor`，看"图谱非空"检查项 |
| 结论恒为"不予受理" | 真实性闸门未过 | 正常行为。确认该主体确实在登记库中；同名主体需用统一社会信用代码 |
| 工具注册数少于 10 | 某个工具注册时抛异常 | 看 govfin 容器日志；注册失败不会影响其他工具 |
| `doctor` 报"运行时图 0 节点" | 尚无决策 | **这是正常的**。运行时图记录决策痕迹，首次决策后才有内容，自检不会因此失败 |
| 本体演化一直提不出新类 | UNK 储备池未达 `GOVFIN_MIN_SUPPORT` | 正常。低频概念留在储备池继续观察是设计行为，不是故障 |
| 容器重建后决策记录丢失 | volume 没挂 | 确认 `govfin-data` volume 已挂到 `/app/data`。决策痕迹是审计依据，丢了无法回答"当初为什么这么判" |

---

## 目录

```
deploy/
├── Dockerfile          # 单镜像双入口：MCP + A2A 共用一份图
├── docker-compose.yml  # 独立部署；接入 Nexent 时把 govfin-agent 段贴过去
├── mcp_config.json     # Nexent MCP 注册配置（容器化导入路径）+ 工具路由表
├── .env.example        # 配置模板
└── README.md
```

**为什么是单镜像双入口**：MCP 和 A2A 共用同一个 `AgentRuntime` 与同一份图数据库。
拆成两个镜像会让两边的图状态分叉——A2A 编排出的决策写进运行时图，
而 MCP 工具查不到，这在溯源场景里是致命的。
