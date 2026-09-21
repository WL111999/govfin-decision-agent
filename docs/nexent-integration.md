# Nexent 集成：契约与实测证据

> **操作步骤见 [`deploy/README.md`](../deploy/README.md)**（10 秒验证 / 独立部署 / 接入 Nexent /
> Skill 注册 / 故障排查）。本文写的是那些步骤**为什么**长这样，以及它们已经被验证到什么程度。

## 一、集成面：三条契约

```
┌────────────────────────────────────────────────────────────────┐
│  Nexent                                                        │
│                                                                │
│   ①  智能体 ──MCP 客户端──► govfin-agent:8930/mcp              │
│        取 10 个领域工具的能力                                    │
│                                                                │
│   ②  Skill 空间 ◄── 导入 dist/*.zip ── 3 份工作流模板            │
│        决定"什么时候按什么顺序调哪些工具"                         │
│                                                                │
│   ③  （可选）A2A 客户端 ──► govfin-agent:8940/                 │
│        直接把整条编排交给 govfin 的 7 智能体拓扑                 │
└────────────────────────────────────────────────────────────────┘
```

三条契约相互独立，可以只用其中一条：

- **只用 ①**：Nexent 自己决定怎么调工具。适合想让平台侧掌控编排的场景。
- **① + ②**：用 Skill 把领域知识（顺序、失败模式、降级策略）固化下来。
  这是**推荐组合**，也是赛题"Skills 编排能力"指向的形态。
- **① + ② + ③**：需要完整编排日志与 A2A 互操作时再加。

## 二、① MCP 工具契约

`streamable-http`，端点 `/mcp`。10 个工具，按四阶段分组：

| 阶段 | 工具 | 入参 | 返回要点 |
|---|---|---|---|
| **检索** | `gov_business_lookup` | `subject*` | 登记记录、注册资本、成立日期、经营范围、`found` |
| | `gov_social_security` | `subject*` | 逐月缴费记录、异常月份（欠缴/断缴） |
| | `gov_judicial_scan` | `subject*` | 行政处罚、涉诉案件 |
| | `fin_financial_parser` | `subject*` | 已对齐本体的财务指标；资产负债率自动计算 |
| | `graph_stats` | — | 节点/边总数、各层规模、本体版本、完整性报告 |
| **推理** | `kg_path_query` | `start*`, `constraint`, `max_hops` | 受约束多跳路径 + 被拒路径及理由 |
| **决策** | `risk_decision` | `subject*`, `persist` | 结论、置信度、逐步中间结论、决策编号 |
| **溯源** | `evidence_bundle` | `decision_id`, `subject` | 采纳/被拒链、条款原文、**依据漂移检测** |
| **演化** | `ontology_status` | — | 本体版本、UNK 候选、待仲裁提案 |
| | `ontology_evolve` | `commit`, `use_llm` | 一轮演化报告 + 版本链 |

`*` = 必填。`subject` 接企业名称或统一社会信用代码。

### 两条必须守住的语义

**`found=false` 与 `ok=false` 不是一回事。**
前者是"查无此主体"（业务事实，应当终止流程），后者是"查询失败"（系统故障，
应当重试或降级）。把两者混为一谈会让一次网络抖动表现为"这家企业不存在"——
在授信场景里这是一个会直接导致误判的错误。

工具返回里两者分别用 `found` 与 `ok` 表达，Skill 的失败模式章节对此有明确处置。

**`risk_decision` 的 `persist` 决定是否留痕。**
`persist=true`（默认）会把完整决策写入 Layer3，之后 `evidence_bundle` 才查得到。
传 `false` 用于试探性计算，代价是这条决策**不可追溯**——因此它不适合任何有
合规要求的场景。

## 三、② Skill 编排契约

三份 Skill 之间是**串行前置**关系，不是三个平级能力：

```
enterprise-authenticity-check   ──通过──►  counterparty-risk-contagion-scan
        （前置闸门）                              （风险画像）
                                                      │
                                                      ▼
                                            credit-decision-chain
                                                （结论合成）
```

| Skill | 输入 | 输出 | 不适用场景（写在 SKILL.md 里） |
|---|---|---|---|
| `enterprise-authenticity-check` | 主体标识 | 登记状态、用工规模真实性 | 批量筛选（本技能为单主体设计） |
| `counterparty-risk-contagion-scan` | 通过闸门的主体 | 关联方风险指标集合 | 闸门未过时 |
| `credit-decision-chain` | 风险指标集合 | 授信结论 + 完整决策链 | **闸门未过时（不应产生任何授信结论）**；批量筛选 |

每个 Skill 都写了 **`allowed-tools`**、适用与**不适用**边界、以及**失败模式与降级策略**。
失败模式那一节是这三份文件区别于普通提示词模板的地方——每一条都对应一个上游系统里
真实会发生的故障，以及该故障下应当怎么做（重试 / 降级 / 转人工 / 终止），
以及**不该怎么做**。

`examples.md` 与 `SKILL.md` 分离，走的是**渐进式披露**：主文件只写常规路径，
示例与边界情形放在 reference 文件里，只在默认用法不够用时才加载。三份主文件的
正文合计约 9,000 字（全文含 YAML frontmatter 约 20KB），而示例文件另有约 15KB
——把示例全部内联，等于每次 Skill 触发都多烧掉一倍多的上下文。

## 四、③ A2A 协作拓扑

`/` 收 JSON-RPC `message/send`；`/.well-known/agent-card.json` 供能力发现。

7 个智能体，1 个 orchestrator + 6 个 worker：

| 阶段 | 智能体 | 技能 |
|---|---|---|
| 检索 | `gov-data-agent` | 工商登记 / 社保 / 司法扫描 |
| 检索 | `fin-data-agent` | 财务与征信解析 |
| 推理 | `kg-reasoning-agent` | 受约束多跳路径搜索 |
| 决策 | `decision-agent` | 授信决策合成 |
| 溯源 | `provenance-agent` | 证据束取回 |
| 编排 | `orchestrator` | 驱动四阶段流程 |

在 Agent Card 里明确写了两条证据策略：

```json
"x-govfin-evidence-policy": "真实性未通过则终止流程，不产生后续结论；阶段间传递的是证据而非摘要"
```

**阶段间传证据、不传摘要**，是这套编排与"把工具串起来调用"的本质区别。编排日志里
每一步都带 `payload` 全量返回，而不是一句"已完成"。摘要是**有损**的，而决策链的每
一步都可能成为事后追责的对象。

## 五、实测证据

以下全部在本机以 `--ingest data` 起服务后实测得到。

### MCP 握手与工具注册

```
initialize -> 200   （session 建立成功）
tools/list -> 10 个工具
  gov_business_lookup / gov_social_security / gov_judicial_scan / fin_financial_parser
  kg_path_query / evidence_bundle / risk_decision / ontology_status / ontology_evolve / graph_stats
```

### 导入与跨域绑定

```json
{"documents": 12, "nodes_created": 32, "edges_created": 89, "tuples_routed_to_unk": 48,
 "entities_merged": 1,
 "binding": {"constrained_edges": 20, "indicator_edges": 3, "threshold_nodes": 2,
             "threshold_dimension_edges": 2, "unmatched_dimensions": [],
             "matches": [{"indicator": "社保缴纳异常(2026-01)", "dimension": "经营稳定性",
                          "clause": "授信指引条款:银保监发〔2024〕12号-§3.2", "matched_span": "社保"}]}}
```

跨域绑定是**自动**发生的：政务侧的"社保缴纳异常"指标自动对上了金融侧的
"经营稳定性"维度，并挂到具体条款上。

### 决策合成

```
verdict: 审慎核定    confidence: 0.620856
rationale: 真实性核验通过。关联方传导命中 1 条判定依据，其中 1 条触发监管阈值：
           连续异常月数上限=3，观测值为 3（同维度风险指标出现 3 次），触发
           （依据：3 ≥ 3，条款 银保监发〔2024〕12号-§4.1）。按条款要求应审慎
           核定授信额度并追加担保。
accepted_paths: 7    rejected_paths: 0
```

### 四阶段编排日志

```
检索 | gov_business_lookup    | ok | 命中 1 条工商登记记录，主体登记在册
检索 | gov_social_security    | ok | 5 条缴费记录中 3 条异常
检索 | gov_judicial_scan      | ok | 命中 1 条行政处罚、0 条涉诉记录
检索 | fin_financial_parser   | ok | 已解析财务指标：注册资本=800.0
推理 | kg_path_query          | ok | 命中 3 条路径
推理 | kg_path_query          | ok | 命中 1 条路径
决策 | risk_decision          | ok | 真实性核验通过。…（触发 连续异常月数上限=3）
溯源 | evidence_bundle        | ok | 取回 7 条采纳链，0 条被拒链，依据条款 1 条，无依据漂移
```

### 本体演化

```
version 0.1.0 -> 0.2.0
clustered 5 | alias_learned 1 | auto_approved ['社保缴纳记录信息']
pending_arbitration ['经营异常名录', '触发'] | 拒绝 2 条（附理由）
本体版本链：[('0.1.0', 'system'), ('0.2.0', 'auto')]
```

## 六、部署形态上的两个决定

**单镜像双入口。** MCP 与 A2A 共用同一个 `AgentRuntime` 与同一份图数据库。
拆成两个镜像会让两边的图状态分叉——**A2A 编排出的决策写进运行时图，而 MCP 工具
查不到**，这在溯源场景里是致命的。

**必须同网部署。** Nexent 容器只能用**服务名**访问 govfin，用 `localhost` 会指向
Nexent 自己。这是接入失败最常见的原因，所以在 `deploy/README.md` 里把它放在最显眼
的位置，并给了一条容器内的连通性自测命令（`docker exec … curl …`）——
**界面上的配置对不对，在这条命令面前是立刻见分晓的。**

## 七、当前状态

全部打通，实测环境为 Docker Desktop 29.8.0 + Nexent v2.6.0 全量部署（12 个容器）。

| 项 | 状态 |
|---|---|
| MCP / A2A 服务端到端 | ✅ 实测通过（见第五节） |
| 10 个工具 / 7 个智能体 / 4 阶段编排 | ✅ 实测通过 |
| 容器镜像构建 | ✅ `govfin-decision-agent:1.0.0`，构建期内置自检全绿 |
| 容器内服务运行 | ✅ `Up (healthy)`，MCP / A2A 双入口正常 |
| govfin 接入 Nexent 网络 | ✅ `docker network connect nexent_network govfin-agent` |
| **从 Nexent 容器内访问 govfin** | ✅ MCP 握手 200、列出 10 个工具；A2A `/health` 返回完整图状态 |
| **Nexent 侧 MCP 注册** | ✅ `govfin-decision`，`enabled=true`，自动发现 10 个工具 |
| **3 份 Skill 导入** | ✅ 全部 HTTP 201 |

### 注册是怎么做的

用脚本走 API，而不是在界面上点：

```bash
python -X utf8 scripts/register_to_nexent.py            # 幂等，可重复跑
python -X utf8 scripts/register_to_nexent.py --list-only # 只看现状
```

**为什么不用界面。** 界面上点完就没了，而注册动作本身也是需要可复现的：换一台机器、
换一套部署，你得重新回忆当时填了什么。脚本留在仓库里，注册了哪些工具、哪些技能、
发现到几个工具，都是可核对的输出。

**这一步的价值不只是省事。** `registry_json._toolNames` 返回的 10 个工具名，是
**Nexent 自己连上 govfin 的 MCP 端点后发现的**，不是我们写进去的声明——也就是说，
这条回执同时证明了三件事：网络通、MCP 协议握手成功、工具清单能被正确解析。

### 与 Skill 的 `allowed-tools` 对齐

Skill 里写的 `allowed-tools` 必须与 Nexent 发现到的工具名**逐字一致**。这一点值得
单独盯着，因为工具名对不上时**不会报错**——只表现为模型不去用那个工具，而"模型选择
不用"和"工具根本不存在"在日志里长得一模一样。

实测发现到的 10 个名字：

```
gov_business_lookup  gov_social_security  gov_judicial_scan  fin_financial_parser
kg_path_query  evidence_bundle  risk_decision  ontology_status  ontology_evolve  graph_stats
```

与 `deploy/mcp_config.json` 的 `expectedTools` 及三份 SKILL.md 的 `allowed-tools` 一致。

### 界面入口

`http://localhost:3000`，超管账号在部署时自动创建（部署日志里会打印邮箱与密码）。
