# mini-feishu-bot

**极简**飞书机器人 + **可插拔** LangGraph 工作流。

- **极简**：核心链路只有两个文件——`main.py`（WebSocket 长连接收发，197 行）+ `simple_agent/workflow.py`（桌面助手，27 行），去除空行与注释合计 224 行。零公网 IP、零回调、零数据库，能上网就能跑。
- **可插拔**：`.env` 里改一个 `QR_WORKFLOW` key，即可切换整条工作流：

| `QR_WORKFLOW` | 案例 |
| --- | --- |
| `simple`（默认） | **万能桌面助手**：deepagents 执行 shell / 读写文件，消息进、答案出 |
| `dev_review` | **研发流程审查**：意图识别 → 备料 → deep agent 审核 → 飞书文档报告 |

## 快速开始

要求：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)、飞书自建应用、智谱 API Key。

1. 飞书应用：开启**机器人**；事件订阅选**长连接**（不要 webhook），订阅 `im.message.receive_v1`；开通 `im:message`（dev_review 另需 docx 读写、wiki 只读、云文档权限）；发版。
2. `cp .env.example .env`，填入：

```dotenv
QR_FEISHU_APP_ID=cli_xxx
QR_FEISHU_APP_SECRET=your_secret
ZHIPU_API_KEY=your_api_key
QR_WORKFLOW=simple        # 或 dev_review
```

3. 启动：

```bash
uv sync
uv run main.py            # 自动读 .env；看到 starting bot 即成功
uv run pytest             # 测试
```

私聊直接发消息，群里需 @机器人。

## 案例 1：万能桌面助手（simple）

deepagents 单 agent，自带 shell 执行与文件读写工具：

> 「看看我的桌面有哪些文件」「把 ~/Downloads 里今天的截图列出来」

## 案例 2：研发流程审查（dev_review）

> @机器人 审核需求 https://xxx.feishu.cn/wiki/xxx 项目 demo

```
classify_intent（意图+需求链接，结构化输出）
    └─ rejected / 暂不支持 ──────────────> refuse（不受理话术）
resolve_project（文本 > 群绑定 > 唯一默认，纯代码零 LLM）
init_workspace ──> clone_code ∥ clone_knowbase ∥ fetch_doc
    │                首次 clone、之后 pull；需求文档按 标题-版本号 缓存，同版本不重拉
review（deep agent 定制提示词，工具根目录=本次工作区）
write_result ──> 创建飞书文档报告，回复链接
```

- 受理后在原消息贴「敲键盘」表情，回复完成自动撤掉；报告链接默认「组织内可阅读」
- 工作区按项目持久复用（`workspaces/<项目>/`），报告按时间戳落盘 `result/`
- 当前实现需求审核；技术方案审核、代码CR、上线步骤审核识别后回复「暂未支持」
- 已知限制：需求文档 `raw_content` 仅纯文本（表格/画板会缺）；意图识别依赖智谱 function calling

项目注册表 `projects.json`（群绑定后免打项目名；仓库留空则跳过克隆）：

```json
{
  "projects": [
    {"name": "demo", "aliases": ["演示项目"], "chat_ids": ["oc_xxx"],
     "code_repo": "git@github.com:org/repo.git", "know_base_repo": ""}
  ]
}
```

## 新增一个 workflow

`workflows/` 下建同名目录（命名空间包，无需 `__init__.py`），实现写在 `workflow.py`（`build(ctx)` + `@register("your_key")`），并把导入加到 `workflows/registry.py` 底部。提示词/状态/图逻辑分文件放（参考 `dev_review/`）；投递由 `main.py` 适配层统一处理，workflow 不感知飞书。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QR_WORKFLOW` | `simple` | 工作流选择 |
| `QR_MODEL_NAME` | `GLM-5.3-Flash` | 模型（智谱 OpenAI 兼容接口） |
| `ZHIPU_BASE_URL` | 智谱官方 | 模型端点，未以 `/v4` 结尾会自动补 `/paas/v4` |
| `QR_WORKSPACE_ROOT` | `workspaces` | dev_review 工作区根目录（gitignore） |
| `QR_PROJECTS_FILE` | `projects.json` | 项目注册表路径 |
| `QR_REPORT_FOLDER_TOKEN` | 空 | 报告文档存放目录（默认云空间根） |
| `QR_REPORT_LINK_SHARE` | `tenant` | 报告链接分享：`tenant` 组织内可阅读 / `off` 应用私有 |
| `QR_FEISHU_DOMAIN` | `https://open.feishu.cn` | 拼报告链接用 |
| `QR_SIMPLE_ROOT` | 用户主目录 | simple 的工具根目录 |

## 部署

进程需常驻，Linux 用 systemd：

```ini
# /etc/systemd/system/mini-feishu-bot.service
[Unit]
Description=mini-feishu-bot
After=network-online.target
[Service]
User=bot
WorkingDirectory=/opt/mini-feishu-bot
EnvironmentFile=/opt/mini-feishu-bot/.env
ExecStart=/opt/mini-feishu-bot/.venv/bin/python main.py
Restart=always
[Install]
WantedBy=multi-user.target
```

更新：`git pull && uv sync && sudo systemctl restart mini-feishu-bot`。

## 项目结构

```
main.py                  飞书收发 + 结果投递（文本 / 报告链接 / 受理表情）
workflows/               命名空间包，无 __init__.py
  registry.py            注册表：QR_WORKFLOW 选择；底部导入各 workflow 触发注册
  base.py                WorkflowResult / MessageContext / WorkflowContext
  simple_agent/          案例 1：万能桌面助手（单模块）
    workflow.py
  dev_review/            案例 2：研发流程审查
    workflow.py          LangGraph 图：节点、路由、构建
    prompts.py           意图/审核提示词、拒绝话术、审核类型表
    state.py             图状态 ReviewState 与意图 schema Intent
feishu_docs.py           飞书文档 API：需求拉取、报告创建、链接分享
projects.json            项目注册表
tests/                   pytest
```
