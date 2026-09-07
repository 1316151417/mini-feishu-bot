"""SimpleAgentWorkflow：单 agent 自由问答，原 workflow.py 实现。"""
from __future__ import annotations

import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

from workflows.base import (MessageContext, Workflow, WorkflowContext, WorkflowResult,
                            final_answer)
from workflows.registry import register


@register("simple")
def build(ctx: WorkflowContext) -> Workflow:
    """电脑助手：文本进、答案出。"""
    home = Path(os.getenv("QR_SIMPLE_ROOT") or Path.home()).expanduser().resolve()
    agent = create_deep_agent(
        model=ctx.model,
        system_prompt=(
            "你是电脑助手，只帮助用户处理问题。使用工具获取事实后，用简洁中文回答。"
            f"文件工具的根目录是用户主目录 {home}，桌面在 Desktop 下。"
            "需要查看文件或系统信息时优先使用 execute 或 read_file；只有用户明确要求时才 write_file。"
        ),
        backend=LocalShellBackend(root_dir=str(home), timeout=30, inherit_env=True),
    )

    def run(text: str, context: MessageContext | None = None) -> WorkflowResult:
        answer = final_answer(agent.invoke({"messages": [{"role": "user", "content": text}]},
                                           {"recursion_limit": 50}))
        return WorkflowResult(action="reply_text", text=answer)

    return run
