"""DevProcessReviewWorkflow 的图状态与意图 schema。"""
from __future__ import annotations

from typing import Annotated, Callable, Literal, TypedDict, operator

from pydantic import BaseModel, Field


class Intent(BaseModel):
    """第一步意图识别的结构化输出。项目解析不在此处，见 resolve_project。"""

    review_type: Literal["requirement_review", "tech_design_review", "code_review",
                         "release_review", "rejected"] = Field(description="审核类型")
    requirement_link: str = Field(default="", description="消息中的飞书需求文档链接，没有则为空")


class ReviewState(TypedDict):
    user_text: str
    chat_id: str
    notify_accepted: Callable[[dict], None] | None  # 受理回执回调，只读不写
    review_type: str
    project_name: str
    requirement_link: str
    project: dict  # 注册表中的项目配置（code_repo / know_base_repo / chat_ids）
    workspace: str
    code_dir: str
    know_base_dir: str
    docs_file: str
    docs_label: str  # 如《导出需求》r7，用于提示词与报告明确「审的是哪份」
    prep_notes: Annotated[list[str], operator.add]  # 并行备料分支共用，必须累加
    review_markdown: str
    reply_text: str
    report_markdown: str
    report_title: str
