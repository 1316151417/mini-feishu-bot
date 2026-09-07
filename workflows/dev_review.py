"""DevProcessReviewWorkflow：研发流程审核（当前实现需求审核）。

LangGraph 管线：意图识别 → 建工作区 → 并行备料（代码/知识库克隆、需求文档拉取）
→ deep agent 审核 → 结果落盘。投递（回复文本或创建报告文档）在适配层完成，
图本身不感知渠道；飞书文档拉取以 doc_fetcher 注入，默认走飞书 API。
"""
from __future__ import annotations

import json
import logging
import operator
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

import feishu_docs
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

from workflows import register
from workflows.base import Workflow, WorkflowContext, WorkflowResult, final_answer

LOG = logging.getLogger("quality-reviewer-lite")

REVIEW_TYPES = {"requirement_review": "需求审核", "tech_design_review": "技术方案审核",
                "code_review": "代码CR", "release_review": "上线步骤审核"}

INTENT_PROMPT = """你是研发流程审核机器人的意图识别器。判断用户消息想要哪种审核类型：
- requirement_review：需求审核
- tech_design_review：技术方案审核
- code_review：代码CR
- release_review：上线步骤审核
- rejected：与上述审核无关的消息

已知项目（project 字段尽量匹配下列名称或别名，消息中未提及项目则留空）：
{projects}

再从消息中提取飞书文档链接（形如 https://xxx.feishu.cn/docx/... 或 https://xxx.feishu.cn/wiki/...）\
填入 requirement_link，没有则留空。

用户消息：{text}"""

REVIEW_PROMPT = """你是资深的需求审核专家，对需求文档做专业审核，输出中文 markdown 报告。

## 审核输入
- 项目：{project}
- 需求文档：{docs_file}（先 read_file 通读全文）
- 代码仓库：{code_status}
- 经验知识仓库：{know_base_status}
- 备料说明：{prep_notes}

## 审核维度
1. 完整性：目标用户、使用场景、功能范围、边界条件、非功能需求是否齐全
2. 清晰性：是否存在歧义、可有多种理解的需求描述
3. 可测性：每条需求是否可验证、验收标准是否明确
4. 一致性：与知识库中的经验、规范是否冲突（若知识库可用）
5. 可行性：结合现有代码结构是否存在明显实现冲突或技术风险（若代码可用）

## 输出格式（严格遵守的 markdown 结构）
# 需求审核报告：{project}

## 审核结论
（通过 / 有条件通过 / 不通过，附一句话理由）

## 问题清单
（按严重程度分级：🔴 阻塞、🟠 重要、🟡 建议；每条注明对应的需求出处）

## 改进建议

## 风险提示

文件工具的根目录是本次审核工作区 {workspace}，只能访问工作区内文件。"""


class Intent(BaseModel):
    """第一步意图识别的结构化输出。"""

    review_type: Literal["requirement_review", "tech_design_review", "code_review",
                         "release_review", "rejected"] = Field(description="审核类型")
    project: str = Field(default="", description="消息中提到的项目名称或别名，未提及则为空")
    requirement_link: str = Field(default="", description="消息中的飞书需求文档链接，没有则为空")


class ReviewState(TypedDict):
    user_text: str
    review_type: str
    project_name: str
    requirement_link: str
    project: dict  # 注册表中的项目配置（code_repo / know_base_repo）
    workspace: str
    code_dir: str
    know_base_dir: str
    docs_file: str
    prep_notes: Annotated[list[str], operator.add]  # 并行备料分支共用，必须累加
    review_markdown: str
    reply_text: str
    report_markdown: str
    report_title: str


def load_projects(path: str | Path) -> dict[str, dict]:
    """读取项目注册表，返回 名称/别名 → 项目配置 的映射。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    index: dict[str, dict] = {}
    for project in data.get("projects", []):
        index[project["name"]] = project
        for alias in project.get("aliases", []):
            index[alias] = project
    return index


def _dir_has_content(path: str) -> bool:
    directory = Path(path)
    return directory.is_dir() and any(directory.iterdir())


def _clone(url: str | None, dest: str, label: str) -> list[str]:
    """克隆仓库到目标目录；未配置或失败记入备料说明，不阻断审核。"""
    if not url:
        return [f"{label}未配置仓库地址，跳过"]
    try:
        result = subprocess.run(["git", "clone", "--quiet", "--depth", "1", url, dest],
                                capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.warning("clone %s failed: %s", url, exc)
        return [f"{label}克隆失败：{exc}"]
    if result.returncode != 0:
        LOG.warning("clone %s failed: %s", url, result.stderr.strip()[:500])
        return [f"{label}克隆失败：{result.stderr.strip()[:200]}"]
    return []


@register("dev_review")
def build(ctx: WorkflowContext) -> Workflow:
    projects = load_projects(ctx.projects_file)
    structured = ctx.model.with_structured_output(Intent)
    fetch_requirement = ctx.doc_fetcher or (
        lambda url: feishu_docs.fetch_doc_content(ctx.feishu_client, url))
    known_projects = "、".join(sorted(projects))

    def classify_intent(state: ReviewState) -> dict:
        intent: Intent | None = structured.invoke(
            INTENT_PROMPT.format(projects=known_projects, text=state["user_text"]))
        if intent is None:
            raise RuntimeError("意图识别未返回结果")
        project = projects.get(intent.project.strip()) or {}
        LOG.info("intent type=%s project=%s link=%s", intent.review_type,
                 project.get("name", ""), bool(intent.requirement_link))
        return {"review_type": intent.review_type, "project_name": project.get("name", ""),
                "requirement_link": intent.requirement_link.strip(), "project": project}

    def route_after_intent(state: ReviewState) -> str:
        if state["review_type"] == "rejected" or state["review_type"] != "requirement_review":
            return "refuse"
        if not state.get("project"):
            return "refuse"
        if not state.get("requirement_link"):
            return "refuse"
        return "init_workspace"

    def refuse(state: ReviewState) -> dict:
        if state.get("reply_text"):
            return {}  # 备料分支已写好失败原因，保留原文
        review_type = state.get("review_type", "rejected")
        if review_type == "rejected":
            text = ("不受理：我只处理研发流程审核，支持需求审核、技术方案审核、代码CR、上线步骤审核。"
                    "请描述审核诉求并附上相关飞书文档链接。")
        elif review_type != "requirement_review":
            text = f"已识别为「{REVIEW_TYPES[review_type]}」，该类型暂未支持，当前仅支持需求审核。"
        elif not state.get("project"):
            text = (f"未能识别项目，已知项目：{known_projects}。"
                    "请在消息中注明项目名称。")
        else:
            text = "未找到需求文档链接，请附上飞书需求文档链接（docx 或 wiki）。"
        return {"reply_text": text}

    def init_workspace(state: ReviewState) -> dict:
        workspace = Path(ctx.workspace_root) / (
            f"{state['project_name']}-{datetime.now():%Y%m%d-%H%M%S}")
        for sub in ("code", "know_base", "docs", "result"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        LOG.info("workspace ready at %s", workspace)
        return {"workspace": str(workspace), "code_dir": str(workspace / "code"),
                "know_base_dir": str(workspace / "know_base"),
                "docs_file": str(workspace / "docs" / "requirement.md")}

    def clone_code(state: ReviewState) -> dict:
        return {"prep_notes": _clone(state["project"].get("code_repo"),
                                     state["code_dir"], "代码仓库")}

    def clone_knowbase(state: ReviewState) -> dict:
        return {"prep_notes": _clone(state["project"].get("know_base_repo"),
                                     state["know_base_dir"], "经验知识仓库")}

    def fetch_doc(state: ReviewState) -> dict:
        try:
            content = fetch_requirement(state["requirement_link"])
            Path(state["docs_file"]).write_text(content, encoding="utf-8")
        except Exception as exc:
            LOG.exception("拉取需求文档失败 %s", state["requirement_link"])
            return {"reply_text": f"需求文档拉取失败：{exc}。请确认链接有效且应用有文档读取权限。"}
        return {}

    def route_after_prep(state: ReviewState) -> str:
        # fetch_doc 失败时写入 reply_text；三个备料分支在此汇合后统一判向
        return "refuse" if state.get("reply_text") else "review"

    def review(state: ReviewState) -> dict:
        workspace = state["workspace"]
        code_status = (f"{state['code_dir']}（已克隆，可用 grep/read_file 查阅）"
                       if _dir_has_content(state["code_dir"]) else "未克隆，跳过可行性核对")
        know_status = (f"{state['know_base_dir']}（已克隆，可检索经验与规范）"
                       if _dir_has_content(state["know_base_dir"]) else "未克隆，跳过一致性核对")
        prep_notes = "\n".join(f"- {note}" for note in state.get("prep_notes", [])) or "- 无"
        agent = create_deep_agent(
            model=ctx.model,
            system_prompt=REVIEW_PROMPT.format(
                project=state["project_name"], docs_file=state["docs_file"],
                code_status=code_status, know_base_status=know_status,
                prep_notes=prep_notes, workspace=workspace),
            backend=LocalShellBackend(root_dir=workspace, timeout=30, inherit_env=True),
        )
        result = agent.invoke(
            {"messages": [{"role": "user",
                           "content": f"请审核需求文档 {state['docs_file']} 并按约定格式输出报告。"}]},
            {"recursion_limit": 50})
        return {"review_markdown": final_answer(result)}

    def write_result(state: ReviewState) -> dict:
        project = state["project"]
        meta = [
            "## 审核信息",
            f"- 项目：{project['name']}",
            "- 审核类型：需求审核",
            f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 需求链接：{state['requirement_link']}",
            f"- 代码仓库：{project.get('code_repo') or '未配置'}",
            f"- 知识库仓库：{project.get('know_base_repo') or '未配置'}",
            *(f"- 备料说明：{note}" for note in state.get("prep_notes", [])),
            "",
        ]
        report = "\n".join(meta) + state["review_markdown"] + "\n"
        (Path(state["workspace"]) / "result" / "review.md").write_text(report, encoding="utf-8")
        return {"report_markdown": report,
                "report_title": f"需求审核报告-{project['name']}-{datetime.now():%Y%m%d-%H%M}"}

    graph = StateGraph(ReviewState)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("refuse", refuse)
    graph.add_node("init_workspace", init_workspace)
    graph.add_node("clone_code", clone_code)
    graph.add_node("clone_knowbase", clone_knowbase)
    graph.add_node("fetch_doc", fetch_doc)
    graph.add_node("review", review)
    graph.add_node("write_result", write_result)
    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges("classify_intent", route_after_intent,
                                ["refuse", "init_workspace"])
    graph.add_edge("init_workspace", "clone_code")
    graph.add_edge("init_workspace", "clone_knowbase")
    graph.add_edge("init_workspace", "fetch_doc")
    graph.add_edge("clone_code", "route_prep")
    graph.add_edge("clone_knowbase", "route_prep")
    graph.add_edge("fetch_doc", "route_prep")
    graph.add_node("route_prep", lambda state: {})
    graph.add_conditional_edges("route_prep", route_after_prep, ["refuse", "review"])
    graph.add_edge("review", "write_result")
    graph.add_edge("write_result", END)
    graph.add_edge("refuse", END)
    app = graph.compile()

    def run(text: str) -> WorkflowResult:
        state = app.invoke({"user_text": text, "prep_notes": []})
        if state.get("report_markdown"):
            return WorkflowResult(
                action="reply_report", report_markdown=state["report_markdown"],
                report_title=state["report_title"],
                metadata={"review_type": state["review_type"],
                          "project": state["project_name"],
                          "workspace": state.get("workspace", ""),
                          "requirement_link": state.get("requirement_link", "")})
        return WorkflowResult(action="reply_text",
                              text=state.get("reply_text") or "未能完成审核，请稍后再试。")

    return run
