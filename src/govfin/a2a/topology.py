"""A2A 协作拓扑：Agent Card 定义与主从编排。

单智能体包打天下在这个场景里是错的。政务数据核验、财务解析、图推理、决策合成、
审计回放对**证据口径**的要求各不相同：核验只认政务原始登记（LLM 抽的边一律不算数），
推理要允许规则推导边参与，审计则必须能拿到每条边的原始片段。把它们塞进一个
Agent 的上下文里，口径边界会被模型的自由发挥一点点磨掉——「这次就用 LLM 抽的边
凑一下吧」是这类系统失效的典型方式。拆成多个 Agent，每个 Agent 的 Card 里写死
自己的证据口径，边界才是硬的。

拓扑是**主从式**，不是对等式：``govfin-orchestrator`` 持有执行计划，
六个 worker 各自只做一件事。这么做是为了让"谁在什么时候用了什么口径"在编排
日志里是可读的。对等式协商看起来更先进，但一条链路上谁把证据降级了，
事后很难从对话记录里看出来。

执行计划分四个阶段，对应比赛要求的「检索-推理」双驱动：

    检索（retrieval）：政务侧与金融侧的事实**并行**采集，互不依赖
    推理（reasoning）：在图上有约束地走多跳，把事实连成可解释的链
    决策（decision）  ：把链收敛成结论
    溯源（provenance）：把结论连同当时的证据冻结留存

阶段之间是**硬串行**的，且检索阶段拿不到政务登记就**不进入推理**。
这不是为了保守，而是因为后续所有推理的起点都是"这个主体真实存在"——
起点没验，整条链再漂亮也是空的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from govfin.runtime import AgentRuntime
from govfin.tools import DomainTools


@dataclass(frozen=True)
class AgentSkill:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class AgentCard:
    name: str
    description: str
    role: str
    evidence_policy: str
    skills: tuple[AgentSkill, ...]
    inputs: str
    outputs: str

    def to_dict(self, *, url: str = "") -> dict:
        card: dict = {
            "name": self.name,
            "description": self.description,
            "version": "1.0.0",
            "protocolVersion": "0.2.6",
            "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [s.to_dict() for s in self.skills],
            "x-govfin-role": self.role,
            "x-govfin-evidence-policy": self.evidence_policy,
            "x-govfin-inputs": self.inputs,
            "x-govfin-outputs": self.outputs,
        }
        if url:
            card["url"] = url
        return card


# --------------------------------------------------------------------------
# 六个 worker 的能力与证据口径
# --------------------------------------------------------------------------

GOV_DATA = AgentCard(
    name="gov-data-agent",
    description="政务数据核验智能体：工商登记、社保缴纳、行政处罚与涉诉记录",
    role="retrieval",
    evidence_policy="只允许政务系统原始登记数据（evidence_class=direct）参与结论，LLM 抽取结果不采信",
    inputs="企业名称或统一社会信用代码",
    outputs="政务事实记录，每条带 source_document / source_locator / snippet",
    skills=(
        AgentSkill(
            id="gov_business_lookup",
            name="工商登记核验",
            description="核验主体是否登记在册，返回登记记录与经营范围",
            tags=["政务", "核验", "真实性"],
            examples=("核验 甲科技有限公司 的工商登记",),
        ),
        AgentSkill(
            id="gov_social_security",
            name="社保缴纳核验",
            description="逐月缴费记录与异常月份（欠缴、断缴）",
            tags=["政务", "社保", "经营稳定性"],
        ),
        AgentSkill(
            id="gov_judicial_scan",
            name="司法与处罚扫描",
            description="行政处罚记录与涉诉案件",
            tags=["政务", "司法", "合规性"],
        ),
    ),
)

FIN_DATA = AgentCard(
    name="fin-data-agent",
    description="金融数据解析智能体：从财务报表与征信报告中解析财务指标",
    role="retrieval",
    evidence_policy="财务指标必须能回指到源文档；缺失的指标如实返回缺失，不做插值估计",
    inputs="企业名称或统一社会信用代码",
    outputs="财务指标键值对 + 来源文档",
    skills=(
        AgentSkill(
            id="fin_financial_parser",
            name="财务数据解析",
            description="解析负债、资产、营收、逾期次数，齐备时计算资产负债率",
            tags=["金融", "财务", "偿债能力"],
            examples=("解析 甲科技有限公司 的财务指标",),
        ),
    ),
)

KG_REASONING = AgentCard(
    name="kg-reasoning-agent",
    description="图谱推理智能体：在三层属性图上做受约束的多跳推理",
    role="reasoning",
    evidence_policy="允许 direct 与 derived 边参与；每条路径记录置信度分解与被拒路径",
    inputs="起点主体 + 路径约束名",
    outputs="推理链集合，每条带逐环证据、置信度分解",
    skills=(
        AgentSkill(
            id="kg_path_query",
            name="受约束多跳推理",
            description="按业务约束搜索推理链；约束决定哪些链能回答问题",
            tags=["图推理", "多跳", "跨文档"],
            examples=(
                "扫描 甲科技有限公司 的关联方风险传导",
                "为 社保缴纳异常 生成授信决策链",
            ),
        ),
    ),
)

DECISION = AgentCard(
    name="decision-agent",
    description="决策合成智能体：把推理链收敛成带阈值的授信结论",
    role="decision",
    evidence_policy="结论必须落在具体监管条款的判定阈值上；取不到阈值的依据不作为结论",
    inputs="企业名称或统一社会信用代码",
    outputs="结论 + 置信度 + 逐步中间结论",
    skills=(
        AgentSkill(
            id="risk_decision",
            name="授信决策合成",
            description="真实性闸门 → 关联方风险传导 → 阈值判定 → 结论",
            tags=["决策", "授信", "阈值"],
            examples=("评估 甲科技有限公司 的授信风险",),
        ),
    ),
)

PROVENANCE = AgentCard(
    name="provenance-agent",
    description="决策溯源智能体：回放决策依据并检测依据漂移",
    role="provenance",
    evidence_policy="只读冻结快照，不重新推理——重新推理得到的是今天的解释，不是当初的依据",
    inputs="决策编号或主体",
    outputs="完整依据链、条款原文、被拒路径、漂移清单",
    skills=(
        AgentSkill(
            id="evidence_bundle",
            name="证据束回放",
            description="取回决策的完整依据链与漂移检测结果",
            tags=["溯源", "审计", "合规"],
            examples=("回放决策 DEC-XXXX 的完整依据",),
        ),
    ),
)

ONTOLOGY = AgentCard(
    name="ontology-agent",
    description="本体演化智能体：低资源领域本体的半自动构建与持续更新",
    role="evolution",
    evidence_policy="是否演化由符号对齐决定；LLM 只能填语义细节，不能凭空造类",
    inputs="（无参数）或 commit 开关",
    outputs="演化报告：新增类/属性、学到的别名、待人工仲裁提案",
    skills=(
        AgentSkill(
            id="ontology_status",
            name="本体状态查询",
            description="本体版本、类属性规模、UNK 储备池待演化候选",
            tags=["本体", "低资源", "演化"],
        ),
        AgentSkill(
            id="ontology_evolve",
            name="触发本体演化",
            description="跑一轮储备池 → 聚类 → 对齐 → 提案 → 验证 → 提交",
            tags=["本体", "神经符号", "演化"],
        ),
    ),
)

ORCHESTRATOR = AgentCard(
    name="govfin-orchestrator",
    description="主控智能体：编排检索-推理-决策-溯源四阶段跨域决策流程",
    role="orchestrator",
    evidence_policy="真实性未通过则终止流程，不产生后续结论；阶段间传递的是证据而非摘要",
    inputs="主体标识（企业名称或统一社会信用代码）",
    outputs="完整决策包：事实、推理链、结论、依据、编排日志",
    skills=(
        AgentSkill(
            id="assess_credit_risk",
            name="跨域授信风险评估",
            description="驱动完整四阶段流程，输出决策包与编排日志",
            tags=["编排", "跨域", "授信", "检索-推理"],
            examples=("评估 甲科技有限公司 的授信风险",),
        ),
        AgentSkill(
            id="explain_decision",
            name="决策解释",
            description="回放一次已作出的决策，给出可追溯依据",
            tags=["编排", "溯源"],
        ),
    ),
)

WORKER_CARDS = (GOV_DATA, FIN_DATA, KG_REASONING, DECISION, PROVENANCE, ONTOLOGY)
ALL_CARDS = (ORCHESTRATOR, *WORKER_CARDS)


def card_for(name: str) -> AgentCard:
    for card in ALL_CARDS:
        if card.name == name:
            return card
    raise KeyError(f"未定义的 Agent: {name!r}；可用: {[c.name for c in ALL_CARDS]}")


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


@dataclass
class OrchestrationStep:
    phase: str
    agent: str
    skill: str
    status: str  # ok | empty | failed | skipped
    summary: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "agent": self.agent,
            "skill": self.skill,
            "status": self.status,
            "summary": self.summary,
        }


@dataclass
class OrchestrationResult:
    task_id: str
    subject: str
    phase_reached: str
    steps: list[OrchestrationStep] = field(default_factory=list)
    decision: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)
    chains: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "subject": self.subject,
            "phase_reached": self.phase_reached,
            "steps": [s.to_dict() for s in self.steps],
            "facts": self.facts,
            "chains": self.chains,
            "decision": self.decision,
            "provenance": self.provenance,
        }


class Orchestrator:
    """主从编排器。每一步都记进 ``steps``，编排过程本身也是可审计的。"""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.rt = runtime
        self.tools = DomainTools(runtime)

    def assess_credit_risk(self, subject: str, *, task_id: str = "") -> OrchestrationResult:
        result = OrchestrationResult(
            task_id=task_id or f"task:{subject}", subject=subject, phase_reached="检索"
        )

        # ---- 检索：政务侧与金融侧事实并行采集，互不依赖 ----
        gov = self._run(
            result, "检索", GOV_DATA.name, "gov_business_lookup",
            lambda: self.tools.gov_business_lookup(subject),
        )
        social = self._run(
            result, "检索", GOV_DATA.name, "gov_social_security",
            lambda: self.tools.gov_social_security(subject),
        )
        judicial = self._run(
            result, "检索", GOV_DATA.name, "gov_judicial_scan",
            lambda: self.tools.gov_judicial_scan(subject),
        )
        financial = self._run(
            result, "检索", FIN_DATA.name, "fin_financial_parser",
            lambda: self.tools.fin_financial_parser(subject),
        )
        result.facts = {
            "工商登记": gov.get("records", []) if gov.get("ok") else [],
            "社保缴纳": social.get("records", []) if social.get("ok") else [],
            "行政处罚": judicial.get("penalties", []) if judicial.get("ok") else [],
            "涉诉": judicial.get("lawsuits", []) if judicial.get("ok") else [],
            "财务指标": financial.get("metrics", {}) if financial.get("ok") else {},
        }

        # 真实性闸门：登记库查无此主体时，后面的推理没有起点可言。
        # 这里不是"尽量继续"，而是明确终止，并把终止原因写进编排日志。
        if not gov.get("ok") or not gov.get("found"):
            result.phase_reached = "检索（真实性闸门未通过）"
            for card in (KG_REASONING, DECISION, PROVENANCE):
                result.steps.append(
                    OrchestrationStep(
                        phase="—", agent=card.name, skill="—", status="skipped",
                        summary="工商登记核验未通过，按流程终止，不进入推理阶段",
                    )
                )
            result.decision = {
                "verdict": "不予受理",
                "confidence": 0.0,
                "rationale": "政务登记库中查无该主体，真实性核验不通过，流程终止。",
            }
            return result

        # ---- 推理：在图上有约束地走多跳 ----
        result.phase_reached = "推理"
        chains: dict[str, Any] = {}
        for constraint in ("关联方风险传导扫描", "企业真实性核验"):
            payload = self._run(
                result, "推理", KG_REASONING.name, "kg_path_query",
                lambda c=constraint: self.tools.kg_path_query(subject, c),
            )
            chains[constraint] = {
                "path_count": payload.get("path_count", 0),
                "truncated": payload.get("truncated", False),
                "top_chains": [
                    {
                        "chain": " → ".join(n["label"] for n in p["nodes"]),
                        "confidence": p["confidence"]["score"],
                        "layers": p["layers_visited"],
                    }
                    for p in (payload.get("paths") or [])[:5]
                ],
            }
        result.chains = chains

        # ---- 决策 ----
        result.phase_reached = "决策"
        decision = self._run(
            result, "决策", DECISION.name, "risk_decision",
            lambda: self.tools.risk_decision(subject),
        )
        result.decision = {
            "decision_id": decision.get("decision_id"),
            "verdict": decision.get("verdict"),
            "confidence": decision.get("confidence"),
            "rationale": decision.get("rationale"),
            "judgements": decision.get("judgements", []),
        }

        # ---- 溯源 ----
        result.phase_reached = "溯源"
        decision_id = str(decision.get("decision_id") or "")
        if decision_id:
            bundle = self._run(
                result, "溯源", PROVENANCE.name, "evidence_bundle",
                lambda: self.tools.evidence_bundle(decision_id=decision_id),
            )
            result.provenance = {
                "decision_id": decision_id,
                "accepted_paths": len(bundle.get("accepted_chains", [])),
                "rejected_paths": len(bundle.get("rejected_chains", [])),
                "clauses": [c["label"] for c in bundle.get("clauses", [])],
                "drift_count": bundle.get("drift_count", 0),
            }
        return result

    def explain_decision(self, decision_id: str, *, task_id: str = "") -> OrchestrationResult:
        result = OrchestrationResult(
            task_id=task_id or f"task:{decision_id}", subject=decision_id, phase_reached="溯源"
        )
        bundle = self._run(
            result, "溯源", PROVENANCE.name, "evidence_bundle",
            lambda: self.tools.evidence_bundle(decision_id=decision_id),
        )
        result.provenance = bundle
        return result

    def _run(
        self,
        result: OrchestrationResult,
        phase: str,
        agent: str,
        skill: str,
        call: Callable[[], dict],
    ) -> dict:
        try:
            payload = call()
        except Exception as exc:  # noqa: BLE001 - 单个 worker 故障降级为步骤失败，不炸掉整个任务
            result.steps.append(
                OrchestrationStep(
                    phase=phase, agent=agent, skill=skill, status="failed",
                    summary=f"{type(exc).__name__}: {exc}",
                )
            )
            return {"ok": False, "error": str(exc)}

        if not payload.get("ok"):
            status, summary = "failed", str(payload.get("error") or "调用失败")
        elif payload.get("found") is False or payload.get("path_count") == 0:
            status = "empty"
            summary = str(payload.get("conclusion") or "查询完成，未命中记录")
        else:
            status = "ok"
            summary = str(
                payload.get("conclusion")
                or payload.get("rationale")
                or _summarize_payload(payload)
            )
        result.steps.append(
            OrchestrationStep(
                phase=phase, agent=agent, skill=skill, status=status,
                summary=summary[:300], payload=payload,
            )
        )
        return payload


def _summarize_payload(payload: dict) -> str:
    """给没有自带结论文的工具凑一句摘要。

    编排日志是给人看"这一步干了什么"的，因此摘要里的数字必须真的是这个工具
    返回的东西。原来这里只认 ``path_count``，其它工具一律落到一个填不上的模板，
    日志里印出"命中 — 条路径"——读者会以为那是渲染坏了，其实是没有信息可印。
    空白摘要比错误摘要更容易被忽略，因为错误至少还会让人回头看。
    """
    if "accepted_chains" in payload:
        parts = [
            f"取回 {len(payload.get('accepted_chains') or [])} 条采纳链",
            f"{len(payload.get('rejected_chains') or [])} 条被拒链",
            f"依据条款 {len(payload.get('clauses') or [])} 条",
        ]
        drift = int(payload.get("drift_count") or 0)
        parts.append(f"依据漂移 {drift} 项" if drift else "无依据漂移")
        return "，".join(parts)
    if "path_count" in payload:
        return f"命中 {payload['path_count']} 条路径"
    return f"{payload.get('skill') or '调用'} 完成"


__all__ = [
    "AgentCard",
    "AgentSkill",
    "OrchestrationResult",
    "OrchestrationStep",
    "Orchestrator",
    "ALL_CARDS",
    "WORKER_CARDS",
    "card_for",
]
