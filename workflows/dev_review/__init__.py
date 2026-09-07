"""DevProcessReviewWorkflow：研发流程审核（当前实现需求审核）。

LangGraph 管线：意图识别 → 项目解析 → 建工作区 → 并行备料（代码/知识库同步、需求文档拉取）
→ deep agent 审核 → 结果落盘。投递（回复文本或创建报告文档）在适配层完成，
图本身不感知渠道；飞书文档拉取以 doc_meta/doc_content 注入，默认走飞书 API。
提示词与拒绝话术见 prompts.py，状态与意图 schema 见 state.py。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from langgraph.graph import END, START, StateGraph

import feishu_docs
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

from workflows import register
from workflows.base import (MessageContext, Workflow, WorkflowContext, WorkflowResult,
                            final_answer)

from .prompts import (INTENT_PROMPT, REFUSE_MISSING_LINK, REFUSE_REJECTED,
                      REFUSE_UNKNOWN_PROJECT, REFUSE_UNSUPPORTED, REVIEW_PROMPT,
                      REVIEW_TYPES)
from .state import Intent, ReviewState

LOG = logging.getLogger("quality-reviewer-lite")


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


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")
    return cleaned or "requirement"


def _sync_repo(url: str | None, dest: str, label: str) -> list[str]:
    """首次 clone，已有仓库则 pull 更新；未配置或失败记入备料说明，不阻断审核。"""
    if not url:
        return [f"{label}未配置仓库地址，跳过"]
    if (Path(dest) / ".git").exists():
        try:
            result = subprocess.run(["git", "-C", dest, "pull", "--quiet"],
                                    capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as exc:
            LOG.warning("pull %s failed: %s", url, exc)
            return [f"{label}更新失败（{exc}），沿用本地版本"]
        if result.returncode != 0:
            LOG.warning("pull %s failed: %s", url, result.stderr.strip()[:500])
            return [f"{label}更新失败，沿用本地版本：{result.stderr.strip()[:200]}"]
        return []
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
    # 必须显式 function_calling：langchain-openai 默认 json_schema，
    # GLM 会返回 ```json 围栏文本导致 Pydantic 解析失败
    structured = ctx.model.with_structured_output(Intent, method="function_calling")
    fetch_meta = ctx.doc_meta or (lambda url: feishu_docs.doc_meta(ctx.feishu_client, url))
    fetch_content = ctx.doc_content or (
        lambda document_id: feishu_docs.fetch_doc_content(ctx.feishu_client, document_id))
    unique_projects = list({p["name"]: p for p in projects.values()}.values())
    text_keys = sorted((key for key in projects if key), key=len, reverse=True)  # 长名优先，防短名误匹配
    known_projects = "、".join(sorted(p["name"] for p in unique_projects))

    def classify_intent(state: ReviewState) -> dict:
        intent: Intent | None = structured.invoke(INTENT_PROMPT.format(text=state["user_text"]))
        if intent is None:
            raise RuntimeError("意图识别未返回结果")
        LOG.info("intent type=%s link=%s", intent.review_type, bool(intent.requirement_link))
        return {"review_type": intent.review_type,
                "requirement_link": intent.requirement_link.strip()}

    def route_after_intent(state: ReviewState) -> str:
        if state["review_type"] != "requirement_review":  # rejected 与暂不支持统一拒绝
            return "refuse"
        return "resolve_project"

    def resolve_project(state: ReviewState) -> dict:
        """分层解析项目（纯代码零 LLM）：文本显式提及 > 群绑定 > 唯一项目默认。"""
        text, chat_id = state["user_text"], state.get("chat_id", "")
        for key in text_keys:
            if key in text:
                project = projects[key]
                LOG.info("project=%s by text mention", project["name"])
                return {"project": project, "project_name": project["name"]}
        if chat_id:
            for project in unique_projects:
                if chat_id in project.get("chat_ids", []):
                    LOG.info("project=%s by chat binding", project["name"])
                    return {"project": project, "project_name": project["name"]}
        if len(unique_projects) == 1:
            LOG.info("project=%s by single default", unique_projects[0]["name"])
            return {"project": unique_projects[0], "project_name": unique_projects[0]["name"]}
        return {}

    def route_after_project(state: ReviewState) -> str:
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
            text = REFUSE_REJECTED
        elif review_type != "requirement_review":
            text = REFUSE_UNSUPPORTED.format(label=REVIEW_TYPES[review_type])
        elif not state.get("project"):
            text = REFUSE_UNKNOWN_PROJECT.format(known_projects=known_projects)
        else:
            text = REFUSE_MISSING_LINK
        return {"reply_text": text}

    def init_workspace(state: ReviewState) -> dict:
        workspace = Path(ctx.workspace_root) / state["project_name"]  # 按项目持久复用
        for sub in ("code", "know_base", "docs", "result"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        LOG.info("workspace ready at %s", workspace)
        if state.get("notify_accepted"):
            state["notify_accepted"]({"project": state["project_name"],
                                      "workspace": str(workspace),
                                      "review_type": state["review_type"]})
        return {"workspace": str(workspace), "code_dir": str(workspace / "code"),
                "know_base_dir": str(workspace / "know_base")}

    def clone_code(state: ReviewState) -> dict:
        return {"prep_notes": _sync_repo(state["project"].get("code_repo"),
                                         state["code_dir"], "代码仓库")}

    def clone_knowbase(state: ReviewState) -> dict:
        return {"prep_notes": _sync_repo(state["project"].get("know_base_repo"),
                                         state["know_base_dir"], "经验知识仓库")}

    def fetch_doc(state: ReviewState) -> dict:
        try:
            meta = fetch_meta(state["requirement_link"])
            docs_file = (Path(state["workspace"]) / "docs"
                         / f"{_safe_filename(meta.title)}-{meta.revision_id}.md")
            label = f"《{meta.title}》r{meta.revision_id}"
            if docs_file.exists():  # 同版本已缓存，正文不再重复拉取
                return {"docs_file": str(docs_file), "docs_label": label,
                        "prep_notes": [f"需求文档{label}与本地缓存同版本，复用现有文件"]}
            docs_file.write_text(fetch_content(meta.document_id), encoding="utf-8")
            return {"docs_file": str(docs_file), "docs_label": label}
        except Exception as exc:
            LOG.exception("拉取需求文档失败 %s", state["requirement_link"])
            return {"reply_text": f"需求文档拉取失败：{exc}。请确认链接有效且应用有文档读取权限。"}

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
                docs_label=state.get("docs_label", ""), code_status=code_status,
                know_base_status=know_status, prep_notes=prep_notes, workspace=workspace),
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
            f"- 需求文档：{state.get('docs_label', '')}（{state['docs_file']}）",
            f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"- 需求链接：{state['requirement_link']}",
            f"- 代码仓库：{project.get('code_repo') or '未配置'}",
            f"- 知识库仓库：{project.get('know_base_repo') or '未配置'}",
            *(f"- 备料说明：{note}" for note in state.get("prep_notes", [])),
            "",
        ]
        report = "\n".join(meta) + state["review_markdown"] + "\n"
        result_file = Path(state["workspace"]) / "result" / (
            f"review-{datetime.now():%Y%m%d-%H%M%S}.md")
        result_file.write_text(report, encoding="utf-8")
        return {"report_markdown": report,
                "report_title": f"需求审核报告-{project['name']}-{datetime.now():%Y%m%d-%H%M}"}

    graph = StateGraph(ReviewState)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("refuse", refuse)
    graph.add_node("resolve_project", resolve_project)
    graph.add_node("init_workspace", init_workspace)
    graph.add_node("clone_code", clone_code)
    graph.add_node("clone_knowbase", clone_knowbase)
    graph.add_node("fetch_doc", fetch_doc)
    graph.add_node("review", review)
    graph.add_node("write_result", write_result)
    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges("classify_intent", route_after_intent,
                                ["refuse", "resolve_project"])
    graph.add_conditional_edges("resolve_project", route_after_project,
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

    def run(text: str, context: MessageContext | None = None) -> WorkflowResult:
        state = app.invoke({"user_text": text, "prep_notes": [],
                            "chat_id": context.chat_id if context else "",
                            "notify_accepted": context.on_accepted if context else None})
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
