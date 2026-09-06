"""deepagents 电脑助手：文本进、答案出。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend


def _answer(result: dict) -> str:
    """create_deep_agent 的最终消息就是要回复给用户的内容。"""
    for message in reversed(result.get("messages", [])):
        if getattr(message, "type", "") == "ai" and getattr(message, "content", ""):
            return message.text
    return "我没有生成可回复的结果。"


def build_workflow(model, *, workspace_root: str | Path | None = None) -> Callable[[str], str]:
    """返回单参数调用器：text 进、answer 出。

    workspace_root 仅供测试或受控部署缩小工具可见范围；默认是当前用户主目录。
    """
    home = Path(workspace_root or Path.home()).expanduser().resolve()
    agent = create_deep_agent(
        model=model,
        system_prompt=(
            "你是电脑助手，只帮助用户处理问题。使用工具获取事实后，用简洁中文回答。"
            f"文件工具的根目录是用户主目录 {home}，桌面在 Desktop 下。"
            "需要查看文件或系统信息时优先使用 execute 或 read_file；只有用户明确要求时才 write_file。"
        ),
        backend=LocalShellBackend(root_dir=str(home), timeout=30, inherit_env=True),
    )

    def run(text: str) -> str:
        return _answer(agent.invoke({"messages": [{"role": "user", "content": text}]},
                                    {"recursion_limit": 50}))

    return run
