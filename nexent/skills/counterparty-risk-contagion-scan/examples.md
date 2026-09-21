# 关联方风险传导扫描 — 参考示例

本文件只在默认使用方式不够用时加载。

## 示例一：完整传导链（跨模态、跨层）

一笔真实的传导：目标企业自身无异常，但实际控制人名下的另一家公司社保断缴。

<code>
scan = kg_path_query(start="企业:甲科技有限公司", constraint="关联方风险传导扫描")
# paths[0]:
#   甲科技有限公司 → 张三 → 乙贸易有限公司 → SS-202603-005 → 社保缴纳异常(2026-03)
#   置信度 0.5589  4 跳
#     ← 实际控制人   ｜direct  ｜甲科技_征信报告.txt        ｜实际控制人: 张三
#     ← 法定代表人   ｜direct  ｜营业执照_乙贸易有限公司.png｜法定代表人: 张三
#     ← 缴纳社保     ｜direct  ｜社保缴纳记录.json          ｜缴纳状态: 断缴
#     ← 触发风险信号 ｜derived ｜社保缴纳记录.json          ｜缴纳状态: 断缴（判定为风险状态：断缴）
</code>

**这条链值得注意的地方**：它横跨了三种模态——企业征信文本、营业执照图片 OCR、
社保结构化数据。中间那跳"张三是乙贸易的法定代表人"来自图片解析结果，
如果 OCR 质量不过关，这条链就断了，而目标企业看起来毫无问题。
这正是多模态统一表征的价值：模态差异被压缩进 `source_locator`，
推理层不需要知道证据来自图片还是 JSON。

## 示例二："有可疑信号但证据不足"与"真没风险"的区分

<code>
scan = kg_path_query(start="企业:丁实业有限公司", constraint="关联方风险传导扫描")

# 情形 A：有链但都没过阈值
# {"paths": [], "rejected_paths": [{"reject_reason": "置信度 0.42 < 阈值 0.50", ...}]}
# → 结论：存在可疑传导信号，证据强度不足以自动判定，转人工复核

# 情形 B：什么都没有
# {"paths": [], "rejected_paths": []}
# → 先查数据完整性，再决定结论
stats = graph_stats()
has_relation_edges = stats["graph"]["by_edge_type"].get("持股", 0) > 0
# 若 has_relation_edges 为假 → 结论："未采集到该主体的关联关系数据"
# 若为真           → 结论："该主体无关联方风险"
</code>

情形 A 与 B 在业务上完全不同：A 是"看到了但不确定"，B 是"什么都没看到"。
把 A 报告成"无风险"是最危险的错误——它把一个已经浮现的信号抹掉了。

## 示例三：收紧 max_hops 重试截断结果

<code>
scan = kg_path_query(start=node, constraint="关联方风险传导扫描")
if scan["truncated"]:
    # 不要直接采信。先确认哪些是已经走完的。
    print(scan["truncated_reason"])   # 例如"展开预算耗尽：200000 次扩展"

    # 收紧重试：两跳以内的传导通常已经覆盖主要风险
    scan = kg_path_query(start=node, constraint="关联方风险传导扫描", max_hops=2)
    note = "本次扫描仅覆盖两跳以内传导，更远距离的关联方未纳入"
</code>

截断意味着**还有没走完的路径**。集团客户、担保圈密集的主体很容易触发。
在结论里如实标注覆盖范围，比给一个看起来完整的短列表更负责。

## 示例四：瓶颈定位

<code>
scan = kg_path_query(start=node, constraint="关联方风险传导扫描")
for path in scan["paths"]:
    breakdown = path["confidence"]
    # breakdown["edge_weights"] 逐环给出权重
    weakest = min(breakdown["edge_weights"].items(), key=lambda kv: kv[1])
    print(f"最弱环节: {weakest[0]} = {weakest[1]:.3f}")
</code>

传导链的置信度被最弱那一环压制。向业务方汇报时说清"哪一跳最弱、弱在哪"，
比只报一个总分有用得多——它直接指出了该去核实哪一份材料。

LLM 派生边（`evidence_class = llm`）的惩罚系数是 2.6，是直接证据的 2.6 倍。
如果瓶颈出现在这类边上，说明该跳的依据是模型推断而非原始记录，
应当去取原始材料印证，而不是接受这个推断。
