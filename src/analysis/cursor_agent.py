"""Cursor Background Agent（cursor_sdk）作为 LLM 分析层的替代调用后端。

跟 llm.py 的 call_llm()（OpenAI 兼容 /chat/completions）不是同一套协议：cursor_sdk
背后是完整的 Cursor 编码 agent（内置 Node bridge + 原生二进制，见 2026-07-15 试跑时
反编译 wheel 得到的结论），不是无副作用的纯文本补全接口。Agent.prompt() 必须给一个
local.cwd，这里固定指向项目仓库外层的隔离沙箱目录（data/.cursor_agent_sandbox/，一个
空目录，已加入 .gitignore），不指向项目仓库本身——避免 agent 意外读写真实代码。
sandbox_options.enabled=True 进一步限制其文件系统/网络访问范围。prompt 末尾额外
追加一句强指令，要求它不使用任何工具、只回答要求的 JSON。

2026-07-15 真实连通性测试记录（scratchpad 一次性脚本，非本文件）：prompt 仅"请只回复
OK"时，耗时 ~17s（服务端 duration_ms 7126），token 消耗 input=11354 output=99
total=12771——绝大部分是 agent 框架自身的系统提示词/工具定义开销，不是我们发送内容的
大小决定的；真正跑批次分析时的 total_tokens 预计仍然会远高于 llm.py 那套 OpenAI 路径
下 estimate_tokens() 估的量级。cache_read_tokens=1318 说明有 prompt caching，具体
连续调用间的缓存命中情况本次未验证。

由于拿不到真实美元金额（cursor_sdk 的 RunResult 只有 token 用量，没有价格字段），本
项目对 Cursor 路径的成本熔断退而求其次，用 run.py 里的调用次数上限（max_calls_per_run）
近似控制花费，不是精确的美元预算控制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions, SandboxOptions

SANDBOX_CWD = str(Path(__file__).resolve().parent.parent.parent / "data" / ".cursor_agent_sandbox")

_NO_TOOLS_SUFFIX = (
    "\n\n重要：只输出上面要求的 JSON 本身，不要使用任何工具，不要读取/搜索/列出/修改任何"
    "文件，不要执行任何命令。"
)


class CursorAgentCallError(RuntimeError):
    """cursor_sdk 请求失败或返回 status=error，跟 llm.py 的 http 异常语义对齐。"""


def call_llm_cursor_agent(
    system: str,
    user: str,
    *,
    api_key: str,
    model: str,
) -> tuple[str, Optional[int]]:
    """返回 (原始响应文本, 本次调用消耗的 token 数或 None)，跟 llm.call_llm() 同构
    （同样是 (raw_text, tokens_used) 二元组），run.py 按 provider 切换调用函数时，
    下游 validate_and_normalize()/入库逻辑不需要跟着分支。
    """
    message = f"{system}\n\n{user}{_NO_TOOLS_SUFFIX}"
    try:
        result = Agent.prompt(
            message,
            AgentOptions(
                api_key=api_key,
                model=model,
                local=LocalAgentOptions(
                    cwd=SANDBOX_CWD,
                    sandbox_options=SandboxOptions(enabled=True),
                ),
            ),
        )
    except CursorAgentError as err:
        raise CursorAgentCallError(f"{err.code}: {err.message}") from err

    if result.status == "error":
        raise CursorAgentCallError(f"cursor agent run 失败，run_id={result.id}")

    tokens_used = result.usage.total_tokens if result.usage else None
    return result.result, tokens_used
