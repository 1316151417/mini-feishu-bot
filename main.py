"""单进程飞书长连接：收消息、后台执行 workflow、按结果类型投递。"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import feishu_docs
import lark_oapi as lark
from langchain_openai import ChatOpenAI
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody

from workflows import available, build_workflow
from workflows.base import WorkflowContext, WorkflowResult

LOG = logging.getLogger("quality-reviewer-lite")


def zhipu_base_url(value: str | None = None) -> str:
    """兼容旧项目的 /api 配置和 OpenAI 兼容的 /paas/v4 配置。"""
    base = (value or os.getenv("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api").rstrip("/")
    return base if base.endswith("/v4") else f"{base}/paas/v4"


class RecentMessages:
    """进程内的固定大小去重集合。"""
    def __init__(self, limit: int = 1000):
        self.limit, self.items, self.set = limit, deque(), set()
        self.lock = threading.Lock()

    def add(self, message_id: str) -> bool:
        with self.lock:
            if message_id in self.set:
                return False
            self.items.append(message_id)
            self.set.add(message_id)
            if len(self.items) > self.limit:
                self.set.remove(self.items.popleft())
            return True


class FeishuBot:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id, self.app_secret = app_id, app_secret
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self.bot_open_id = self._bot_open_id()
        self.seen = RecentMessages()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant")
        model = ChatOpenAI(
            model=os.getenv("QR_MODEL_NAME", "GLM-5.3-Flash"),
            api_key=os.environ["ZHIPU_API_KEY"],
            base_url=zhipu_base_url(),
            temperature=0,
            timeout=180,
        )
        self.workflow = self._build_workflow(model)

    def _build_workflow(self, model):
        ctx = WorkflowContext(
            model=model,
            feishu_client=self.client,
            workspace_root=os.getenv("QR_WORKSPACE_ROOT", "workspaces"),
            projects_file=os.getenv("QR_PROJECTS_FILE", "projects.json"),
        )
        name = os.getenv("QR_WORKFLOW", "simple")
        LOG.info("workflow=%s (available: %s)", name, ", ".join(available()))
        return build_workflow(ctx, name)

    def _bot_open_id(self) -> str:
        """SDK 未封装 bot/v3/info 的类型化接口，借通用请求复用客户端的鉴权与 token 缓存。"""
        request = (lark.BaseRequest.builder()
                   .http_method(lark.HttpMethod.GET)
                   .uri("/open-apis/bot/v3/info")
                   .token_types({lark.AccessTokenType.TENANT})
                   .build())
        data = json.loads(self.client.request(request).raw.content)
        # bot/v3/info 的 bot 对象位于顶层；部分旧版响应才放在 data 内。
        bot = data.get("bot") or data.get("data", {}).get("bot") or {}
        open_id = bot.get("open_id", "")
        if not open_id:
            raise RuntimeError(f"无法读取机器人 open_id：{data.get('msg', 'unknown error')}")
        return open_id

    def accept(self, message: Any, sender_type: str) -> str | None:
        if sender_type != "user":
            LOG.info("ignore non-user message id=%s", message.message_id)
            return None
        if message.message_type != "text":
            LOG.info("ignore non-text message id=%s type=%s", message.message_id, message.message_type)
            return None
        if not self.seen.add(message.message_id):
            LOG.info("ignore duplicate message id=%s", message.message_id)
            return None
        try:
            text = json.loads(message.content).get("text", "").strip()
        except (TypeError, json.JSONDecodeError):
            return None
        if message.chat_type == "group":
            mentions = message.mentions or []
            bot = next((m for m in mentions if m.id and m.id.open_id == self.bot_open_id), None)
            if not bot:
                LOG.info("ignore group message id=%s without bot mention", message.message_id)
                return None
            text = text.replace(bot.key or "", "").strip()
        return text or None

    def handle_event(self, data: P2ImMessageReceiveV1) -> None:
        message = data.event.message
        LOG.info("received message id=%s chat=%s type=%s", message.message_id, message.chat_type,
                 message.message_type)
        text = self.accept(message, data.event.sender.sender_type)
        if text:
            LOG.info("submit message id=%s", message.message_id)
            self.executor.submit(self._process, message.message_id, text)

    def _process(self, message_id: str, text: str) -> None:
        try:
            result = self.workflow(text)
            answer = self.deliver(result)
        except Exception as exc:
            LOG.exception("workflow failed for message id=%s", message_id)
            answer = f"处理时出现问题：{type(exc).__name__}。请稍后再试。"
        self.reply(message_id, answer)

    def deliver(self, result: WorkflowResult) -> str:
        """结果投递：审核报告创建飞书文档回复链接，其余直接回复文本。"""
        if result.action == "reply_text":
            return result.text
        try:
            link = feishu_docs.create_report_doc(
                self.client, result.report_title, result.report_markdown,
                folder_token=os.getenv("QR_REPORT_FOLDER_TOKEN") or None)
        except Exception:
            LOG.exception("创建报告文档失败，回退为全文回复")
            return result.report_markdown
        return f"✅ {result.report_title}\n报告：{link}"

    def reply(self, message_id: str, text: str) -> None:
        for index in range(0, len(text), 3000):
            body = ReplyMessageRequestBody.builder().msg_type("text").content(
                json.dumps({"text": text[index:index + 3000]}, ensure_ascii=False)
            ).build()
            request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
            response = self.client.im.v1.message.reply(request)
            if not response.success():
                LOG.error("reply failed: %s", response.msg)
                return

    def run(self) -> None:
        dispatcher = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
            self.handle_event
        ).build()
        lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher,
                       log_level=lark.LogLevel.INFO).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = FeishuBot(os.environ["QR_FEISHU_APP_ID"], os.environ["QR_FEISHU_APP_SECRET"])
    LOG.info("starting bot with open_id=%s", bot.bot_open_id)
    bot.run()


if __name__ == "__main__":
    main()
