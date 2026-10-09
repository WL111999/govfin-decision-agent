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
