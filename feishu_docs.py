"""飞书文档 API 封装：拉取需求文档、创建审核报告文档。"""
from __future__ import annotations

import logging
import os
import re

import lark_oapi as lark
from lark_oapi.api.docx.v1.model.block import Block
from lark_oapi.api.docx.v1.model.create_document_block_children_request import (
    CreateDocumentBlockChildrenRequest,
)
from lark_oapi.api.docx.v1.model.create_document_block_children_request_body import (
    CreateDocumentBlockChildrenRequestBody,
)
from lark_oapi.api.docx.v1.model.create_document_request import CreateDocumentRequest
from lark_oapi.api.docx.v1.model.create_document_request_body import (
    CreateDocumentRequestBody,
)
from lark_oapi.api.docx.v1.model.raw_content_document_request import (
    RawContentDocumentRequest,
)
from lark_oapi.api.docx.v1.model.text import Text
from lark_oapi.api.docx.v1.model.text_element import TextElement
from lark_oapi.api.docx.v1.model.text_element_style import TextElementStyle
from lark_oapi.api.docx.v1.model.text_run import TextRun
from lark_oapi.api.wiki.v2.model.get_node_space_request import GetNodeSpaceRequest

LOG = logging.getLogger("quality-reviewer-lite")

WIKI_TOKEN = re.compile(r"/wiki/([A-Za-z0-9]+)")
DOCX_TOKEN = re.compile(r"/docx/([A-Za-z0-9]+)")

# 审核报告 markdown 子集对应的 docx block_type
_BLOCK_TYPE = {"text": 2, "heading1": 3, "heading2": 4, "heading3": 5,
               "bullet": 12, "ordered": 13, "code": 14}
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BATCH_SIZE = 50  # children 接口单次追加的 block 数上限


def _document_id(client: "lark.Client", url: str) -> str:
    """从飞书链接解析文档 id；wiki 链接先解析到实际文档 token。"""
    if match := WIKI_TOKEN.search(url):
        resp = client.wiki.v2.space.get_node(
            GetNodeSpaceRequest.builder().token(match.group(1)).build())
        node = resp.data.node if resp.success() and resp.data else None
        if node is None or node.obj_type != "docx":
            raise RuntimeError(f"无法解析知识库节点或节点不是文档：{url}")
        return node.obj_token
    if match := DOCX_TOKEN.search(url):
        return match.group(1)
    raise ValueError(f"不是可识别的飞书文档链接（仅支持 docx/wiki）：{url}")


def fetch_doc_content(client: "lark.Client", url: str) -> str:
    """拉取飞书文档纯文本内容。raw_content 不含表格、画板等结构。"""
    resp = client.docx.v1.document.raw_content(
        RawContentDocumentRequest.builder().document_id(_document_id(client, url)).lang(0).build())
    if not resp.success():
        raise RuntimeError(f"接口返回失败：{resp.msg}")
    content = (resp.data.content if resp.data and resp.data.content else "").strip()
    if not content:
        raise RuntimeError("文档内容为空")
    return content


def _run(content: str, *, bold: bool = False, inline_code: bool = False) -> TextElement:
    run = TextRun.builder().content(content).build()
    if bold or inline_code:
        run.text_element_style = (TextElementStyle.builder()
                                  .bold(bold or None).inline_code(inline_code or None).build())
    return TextElement.builder().text_run(run).build()


def _append_plain(elements: list[TextElement], text: str) -> None:
    pos = 0
    for match in _INLINE_CODE.finditer(text):
        if text[pos:match.start()]:
            elements.append(_run(text[pos:match.start()]))
        elements.append(_run(match.group(1), inline_code=True))
        pos = match.end()
    if text[pos:]:
        elements.append(_run(text[pos:]))


def _runs(line: str) -> list[TextElement]:
    """把 **粗体** 与 `行内代码` 拆成多个 text_run 元素。"""
    elements: list[TextElement] = []
    pos = 0
    for match in _BOLD.finditer(line):
        _append_plain(elements, line[pos:match.start()])
        elements.append(_run(match.group(1), bold=True))
        pos = match.end()
    _append_plain(elements, line[pos:])
    return elements


def _block(kind: str, content: str) -> Block:
    block = Block()
    block.block_type = _BLOCK_TYPE[kind]
    setattr(block, kind, Text.builder().elements(_runs(content)).build())
    return block


def markdown_to_blocks(markdown: str) -> list[Block]:
    """把报告 markdown 子集（标题/段落/有序无序列表/代码块/粗体/行内代码）转为 docx block。"""
    blocks: list[Block] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(_block("text", "\n".join(paragraph)))
            paragraph.clear()

    for line in markdown.splitlines():
        if in_code:
            if line.strip().startswith("```"):
                blocks.append(_block("code", "\n".join(code_lines)))
                code_lines.clear()
                in_code = False
            else:
                code_lines.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            in_code = True
        elif stripped.startswith("### "):
            flush_paragraph()
            blocks.append(_block("heading3", stripped[4:]))
        elif stripped.startswith("## "):
            flush_paragraph()
            blocks.append(_block("heading2", stripped[3:]))
        elif stripped.startswith("# "):
            flush_paragraph()
            blocks.append(_block("heading1", stripped[2:]))
        elif re.match(r"^[-*] ", stripped):
            flush_paragraph()
            blocks.append(_block("bullet", stripped[2:]))
        elif re.match(r"^\d+[.、] ", stripped):
            flush_paragraph()
            blocks.append(_block("ordered", re.sub(r"^\d+[.、] ", "", stripped)))
        elif stripped:
            paragraph.append(stripped)
        else:
            flush_paragraph()
    if code_lines:
        blocks.append(_block("code", "\n".join(code_lines)))
    flush_paragraph()
    return blocks


def create_report_doc(client: "lark.Client", title: str, markdown: str,
                      *, folder_token: str | None = None) -> str:
    """创建飞书文档并写入 markdown 报告，返回文档链接。"""
    body = CreateDocumentRequestBody.builder().title(title).build()
    if folder_token:
        body.folder_token = folder_token
    resp = client.docx.v1.document.create(
        CreateDocumentRequest.builder().request_body(body).build())
    if not resp.success() or not resp.data.document:
        raise RuntimeError(f"创建报告文档失败：{resp.msg}")
    document_id = resp.data.document.document_id
    blocks = markdown_to_blocks(markdown)
    for start in range(0, len(blocks), _BATCH_SIZE):
        request = (CreateDocumentBlockChildrenRequest.builder()
                   .document_id(document_id)
                   .block_id(document_id)  # 追加到页面根 block，省略 index 即接在末尾
                   .request_body(CreateDocumentBlockChildrenRequestBody.builder()
                                 .children(blocks[start:start + _BATCH_SIZE]).build())
                   .build())
        child_resp = client.docx.v1.document_block_children.create(request)
        if not child_resp.success():
            raise RuntimeError(f"写入报告内容失败：{child_resp.msg}")
    domain = os.getenv("QR_FEISHU_DOMAIN", "https://open.feishu.cn").rstrip("/")
    link = f"{domain}/docx/{document_id}"
    LOG.info("report doc created: %s (%d blocks)", link, len(blocks))
    return link
