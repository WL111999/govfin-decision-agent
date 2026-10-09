"""导入包必须能通过 Nexent **前端**的校验——这在后端测试里是看不见的。

**为什么要单独测这个。** ``scripts/build_nexent_bundle.py`` 生成的包，后端 API
接受、界面却拒收，而且报错文案指不到真正的原因。

实测踩到的那个坑：包里的占位 ``agent_id`` 取 0，前端校验写的是

    if (!agentData.agent_id || !agentData.agent_info)

而 JavaScript 里 ``!0 === true`` —— 0 被判成"格式错误"，界面弹出一句
"文件类型错误，请检查JSON格式"。后端完全接受 0（它只拿 ``str(agent_id)``
去 ``agent_info`` 里查表，导完再映射成新 id）。

也就是说：**同一份文件 API 导得进去、界面导不进去**，而界面才是用户实际会用的
那条路。这个差异在后端的所有测试里都不存在，只有照着前端的规则演一遍才查得出来。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_bundle_script():
    """加载生成脚本。

    它不是包的一部分（在 scripts/ 下），所以没法直接 import——用 importlib
    按路径加载。脚本的 main() 有 ``if __name__ == "__main__"`` 保护，导入安全。
    """
    path = _ROOT / "scripts" / "build_nexent_bundle.py"
    spec = importlib.util.spec_from_file_location("_nexent_bundle", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_nexent_bundle"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle_script():
    return _load_bundle_script()


def _payload_like_main(module, *, agent_id=None):
    """按 main() 的方式组装一个 payload，但工具列表用固定值（不联网）。"""
    tools = [
        {"class_name": "risk_decision", "name": "risk_decision", "description": "",
         "inputs": None, "output_type": "object", "params": {}, "source": "mcp",
         "usage": module.MCP_SERVER_NAME},
    ]
    info = module.build_agent_info(tools, ["demo-skill"])
    ident = module.PLACEHOLDER_AGENT_ID if agent_id is None else agent_id
    info["agent_id"] = ident
    return {
        "agent_id": ident,
        "agent_info": {str(ident): info},
        "mcp_info": [{"mcp_server_name": module.MCP_SERVER_NAME, "mcp_url": module.MCP_URL}],
    }


def test_placeholder_agent_id_is_truthy(bundle_script):
    """占位 agent_id 必须是真值。

    0 在后端毫无问题、在前端必然被拒——这个常量值得单独钉住，因为它看起来
    完全合理（"没有 id 就填 0"），改回去不会有任何本地测试变红。
    """
    assert bundle_script.PLACEHOLDER_AGENT_ID, (
        "PLACEHOLDER_AGENT_ID 是假值；JavaScript 的 !0 === true，"
        "界面会把包判成格式错误"
    )


def test_generated_payload_passes_frontend_validation(bundle_script):
    """正常生成的 payload 应当零问题。"""
    problems = bundle_script.ui_would_accept(_payload_like_main(bundle_script))
    assert not problems, f"生成的包会被界面拒收：{problems}"


def test_frontend_check_actually_rejects_zero_agent_id(bundle_script):
    """把 agent_id 换成 0 必须被自查捕获——否则这道检查就是摆设。

    没有这条，"自查通过"只能证明这个函数从不报错，不能证明它盯得住东西。
    """
    problems = bundle_script.ui_would_accept(_payload_like_main(bundle_script, agent_id=0))
    assert problems, "agent_id=0 会被 Nexent 前端拒绝，自查却没有报出来"
    assert any("agent_id" in p for p in problems), f"报出的问题里没提到 agent_id：{problems}"


def test_frontend_check_rejects_tool_less_agent(bundle_script):
    """没有工具的智能体也要被拦下。

    它导得进去，但用户在界面上点开才会发现"这个智能体什么也做不了"——
    而那时已经不知道是包的问题还是自己漏勾了。
    """
    payload = _payload_like_main(bundle_script)
    for entry in payload["agent_info"].values():
        entry["tools"] = []
    problems = bundle_script.ui_would_accept(payload)
    assert any("tools" in p for p in problems), f"没有工具的包没被拦下：{problems}"


def test_frontend_check_rejects_dangling_root_id(bundle_script):
    """顶层 agent_id 必须在 agent_info 里有对应条目。

    后端从顶层 agent_id 出发、按 str(agent_id) 查 agent_info，对不上直接 KeyError，
    而对外只报一句 "Agent import error."——从那里反推不出是哪里对不上。
    """
    payload = _payload_like_main(bundle_script)
    payload["agent_id"] = 99
    problems = bundle_script.ui_would_accept(payload)
    assert any("agent_id" in p for p in problems), f"悬空的顶层 id 没被拦下：{problems}"


# --------------------------------------------------------------------------
# 提示词里的调用格式约束
#
# 实测踩到的坑：模型第一轮正确写了 `<code>` 块，第二轮改用 DeepSeek 的原生工具调用
# 标记（`<｜｜DSML｜｜ calls>`），而 Nexent 只认 `<code>`——那串标记没被解析，直接
# 成了"最终答案"显示给用户。整轮流程就此中断，用户看到的是一堆乱码标记。
#
# 提示词在这个系统里**就是接口**：模型怎么写工具调用，完全由它决定。所以约束它
# 和约束代码一样重要——而且更容易被无意改掉（改提示词不会让任何代码测试变红）。
# --------------------------------------------------------------------------


def _complete_code_blocks(text: str) -> list[str]:
    """取出所有**完整**的 `<code>…</code>` 块的内容。

    两个坑，都踩过：

    1. 不能只数 `<code>` 出现几次——那样"有开标签没闭标签"也算数，而模型拿到
       半截标记只会更困惑。这里配对取整块。
    2. **要跳过反引号里的 `` `<code>` ``**。提示词正文里提到这个标记时会把它写成
       行内代码（带反引号），朴素查找会把它当成真的开标签，然后和后面真正的
       `</code>` 配成一对，把中间一大段正文都当成"代码块"剥掉。
    """
    blocks: list[str] = []
    cursor = 0
    while True:
        start = text.find("<code>", cursor)
        if start < 0:
            break
        # 前面紧跟反引号的是行内提及，不是真的代码块
        if start > 0 and text[start - 1] == "`":
            cursor = start + len("<code>")
            continue
        end = text.find("</code>", start)
        if end < 0:
            break
        blocks.append(text[start + len("<code>") : end])
        cursor = end + len("</code>")
    return blocks


def test_constraint_prompt_mandates_code_block_calls(bundle_script):
    """必须**明确要求**用 `<code>` 块，给出完整示例，并点名禁止标记式调用。

    三条缺一不可，而且前两条容易混为一谈：

    - **示例**告诉模型"长这样"；
    - **明确要求**告诉模型"你必须这样"。

    只有示例没有要求时，模型把它当成"众多写法之一"，遇到别的格式诱惑
    （比如它自己原生的工具调用标记）就可能换过去——这正是实测发生的事。
    所以这里把示例块从正文里剥掉之后，还要再查一遍正文里有没有这条要求。
    """
    prompt = bundle_script.CONSTRAINT_PROMPT

    blocks = _complete_code_blocks(prompt)
    assert blocks, "约束提示词里没有完整的 <code>…</code> 示例块"

    callable_blocks = [b for b in blocks if "(" in b and "print(" in b]
    assert callable_blocks, (
        "约束提示词里的代码块没有给出'调用工具并 print'的完整示例——"
        "模型只能自己猜调用格式，而它猜错过"
    )

    # 把示例块剥掉，剩下的才是"要求"部分
    prose = prompt
    for block in blocks:
        prose = prose.replace(f"<code>{block}</code>", "")

    # 只查"正文里出现过 <code>"是不够的——正文里可能只是在**描述历史**
    # （"模型先正确写了一轮 <code>…"），那不是在提要求。所以要求：
    # 至少有一行同时含 <code> 和祈使词，也就是一条真正的指令。
    mandates = [
        line for line in prose.splitlines()
        if "<code>" in line and any(w in line for w in ("必须", "务必", "应当", "要求", "只能"))
    ]
    assert mandates, (
        "正文里没有一条明确要求使用 <code> 块的指令——只有示例和描述时，"
        "模型会把它当成可选的写法之一，遇到自己熟悉的其他格式就可能换过去"
    )

    for marker in ("DSML", "tool_call"):
        assert marker in prompt, (
            f"约束提示词没有点名禁止 {marker} 式的工具调用标记——"
            "模型会退回到它的原生格式，而那串标记不会被解析"
        )


def test_few_shots_show_real_code_blocks(bundle_script):
    """示例里要给出**多处**可直接照抄的 `<code>` 块。

    few-shots 是模型模仿的对象。只给"结论长这样"而不给"怎么调用工具"，
    模型在调用格式上就只能靠自己猜——而它猜错过。

    这里要求至少 4 个**完整**块（真实调用序列分四轮：核验 / 采集 / 推理决策 /
    取依据），而不是数 `<code>` 字符串出现几次。
    """
    shots = bundle_script.FEW_SHOTS_PROMPT
    blocks = _complete_code_blocks(shots)
    assert len(blocks) >= 4, (
        f"few-shots 里完整的 <code> 块只有 {len(blocks)} 个，"
        "覆盖不了真实调用序列；模型会自己补格式"
    )

    callable_blocks = [b for b in blocks if "print(" in b]
    assert callable_blocks, "示例里没有 print，模型可能以为返回值会自动可见"

    tool_named = [b for b in blocks if "gov_" in b or "risk_decision" in b]
    assert tool_named, "示例里的代码块没有调用任何真实工具"


def test_duty_prompt_avoids_parallel_executor(bundle_script):
    """提示词不该引导模型去用执行器工具。

    `parallel_executor` 的参数是嵌套结构，模型写错过一次就整轮中断，
    而这几个工具都很快、顺序调用完全够用——收益远小于风险。
    """
    prompt = bundle_script.CONSTRAINT_PROMPT + bundle_script.DUTY_PROMPT
    assert "parallel_executor" in prompt, (
        "没有显式劝阻 parallel_executor；模型会自己想到它（它确实在工具列表里）"
    )
    assert "不要用" in prompt or "顺序调用" in prompt, "劝阻的措辞不够明确"
