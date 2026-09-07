"""可插拔 workflow 的公共接口与构建上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

from langchain_core.language_models import BaseChatModel

if TYPE_CHECKING:
    import lark_oapi as lark


@dataclass
class WorkflowResult:
    """workflow 的统一输出；投递方式（直接回复 or 创建报告文档）由适配层决定。"""

    action: Literal["reply_text", "reply_report"]
    text: str = ""  # action=reply_text：直接回复的内容
    report_markdown: str = ""  # action=reply_report：报告全文（受限 markdown 子集，供转飞书文档）
    report_title: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class MessageContext:
    """消息来源上下文（群绑定等项目路由用）；API 触发时可传 None。

    on_accepted：受理通过（意图、项目、链接齐备）后的回调，适配层用它发即时回执。
    """

    chat_id: str = ""
    chat_type: str = ""
    on_accepted: Callable[[dict], None] | None = None


@dataclass
class WorkflowContext:
    """构建 workflow 时注入的外部依赖，测试中可整体替换。"""

    model: BaseChatModel
    feishu_client: "lark.Client | None" = None
    workspace_root: str = "workspaces"
    projects_file: str = "projects.json"
    # 需求文档两个拉取动作，默认走飞书 API：元信息（标题+版本号）与正文
    doc_meta: "Callable[[str], object] | None" = None
    doc_content: "Callable[[str], str] | None" = None


Workflow = Callable[[str, MessageContext | None], WorkflowResult]


def final_answer(result: dict) -> str:
    """取 deepagents 结果中最后一条有内容的 ai 消息作为最终回答。"""
    for message in reversed(result.get("messages", [])):
        if getattr(message, "type", "") == "ai" and getattr(message, "content", ""):
            return message.text
    return "我没有生成可回复的结果。"
