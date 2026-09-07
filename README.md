# mini-feishu-bot

一个极简的飞书机器人：你在飞书里发消息，它背后是一个由 **LangGraph + LangChain**（通过 [deepagents](https://github.com/langchain-ai/deepagents)）驱动的 Agent，可以在运行它的机器上执行 shell 命令、读写文件，然后把结果直接回复到飞书。

机器人内置**可插拔 workflow**，通过环境变量 `QR_WORKFLOW` 切换：

| key | workflow | 说明 |
| --- | --- | --- |
| `simple`（默认） | SimpleAgentWorkflow | 电脑助手：消息进、答案出 |
| `dev_review` | DevProcessReviewWorkflow | 研发流程审核：意图识别 → 备料 → deep agent 审核 → 飞书文档报告 |

## 特性

- **零回调、零公网**：使用飞书官方 SDK 的 **WebSocket 长连接**接收事件，不需要公网 IP、域名和 HTTPS 回调地址，在任何能上网的机器上都能跑。
- **可插拔 workflow**：所有 workflow 实现同一接口（文本进、`WorkflowResult` 出），在 `workflows/` 下注册即可接入；审核管线不感知渠道，后续 FastAPI 也能直接触发。
- **研发流程审核**：LangGraph 状态机编排——意图识别（审核类型/项目/需求链接）→ 建工作区并行备料（克隆代码/知识库、拉取需求文档）→ deep agent 按定制提示词审核 → 结果落盘并生成飞书文档报告。
- **模型可换**：默认通过智谱 OpenAI 兼容接口调用 `GLM-5.3-Flash`，改环境变量即可换模型。
- **细节处理**：消息去重、群里只响应 @、长回复自动分段（3000 字/条）、执行异常兜底回复。

## 工作原理

```
飞书用户 ──(WebSocket 长连接)──> lark-oapi 事件
    │
    ▼
main.py  过滤(仅文本、去重、群里需 @机器人)
    │  ThreadPoolExecutor 后台执行，不阻塞事件循环
    ▼
workflows/ 注册表(QR_WORKFLOW 选择)
    ├─ SimpleAgentWorkflow    deepagents 电脑助手
    └─ DevProcessReviewWorkflow  LangGraph 状态机：
    │     ① classify_intent   意图识别：审核类型/项目/需求链接(结构化输出)
    │     ② init_workspace    建 workspace/{code,know_base,docs,result}
    │     ③ 并行备料          git clone 代码/知识库、飞书 API 拉需求文档
    │     ④ review            deep agent 定制提示词审核(工具根目录=本次 workspace)
    │     ⑤ write_result      审核报告写入 workspace/result/review.md
    ▼
WorkflowResult ── 投递(适配层，图不感知渠道)
    ├─ reply_text    直接分段回复(不受理/暂不支持/失败兜底)
    └─ reply_report  创建飞书文档写入报告，回复文档链接
```

ChatOpenAI（智谱 GLM）承担全部模型调用；意图识别用 `with_structured_output`，审核用 deepagents（LangGraph 状态机 + LangChain 工具调用）。

## 快速开始

### 前置要求

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)（包管理器）
- 一个飞书自建应用，以及智谱 API Key

### 1. 创建飞书应用

1. 打开[飞书开放平台](https://open.feishu.cn/) → 创建企业自建应用，记下 **App ID** 和 **App Secret**。
2. 「添加应用能力」→ 开启**机器人**。
3. 「事件与回调」→ 订阅方式选择**长连接**（不要选 webhook），添加事件 `im.message.receive_v1`（接收消息）。
4. 「权限管理」→ 开通：
   - **获取与发送单聊、群组消息**（`im:message`）——收发消息；
   - 使用 `dev_review` workflow 还需要：**查看文档**（docx 只读，拉取需求文档）、**查看知识库**（wiki 只读，解析 wiki 链接）、**创建及编辑文档**（docx 写权限，生成审核报告）。
5. 发布应用版本并等待管理员通过（个人测试空间可自审自批）。

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
QR_FEISHU_APP_ID=cli_xxx            # 飞书应用 App ID
QR_FEISHU_APP_SECRET=your_secret    # 飞书应用 App Secret
ZHIPU_API_KEY=your_api_key          # 智谱 API Key
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
QR_MODEL_NAME=GLM-5.3-Flash         # 可换成其他 GLM 模型
QR_WORKFLOW=simple                   # simple / dev_review
```

可选变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QR_WORKSPACE_ROOT` | `workspaces` | dev_review 工作区根目录（已 gitignore） |
| `QR_PROJECTS_FILE` | `projects.json` | 项目注册表路径 |
| `QR_REPORT_FOLDER_TOKEN` | 空（云空间根目录） | 审核报告文档的存放目录 |
| `QR_FEISHU_DOMAIN` | `https://open.feishu.cn` | 拼接报告链接用的域名 |
| `QR_SIMPLE_ROOT` | 用户主目录 | simple workflow 的工具根目录（收窄权限用） |

### 3. 安装依赖并运行

```bash
uv sync                            # 按 uv.lock 安装依赖到 .venv
uv run --env-file .env main.py     # 或 QR_WORKFLOW=dev_review 启用审核 workflow
uv run pytest                      # 跑测试
```

看到 `starting bot with open_id=...` 即启动成功。

## 研发流程审核（dev_review）

私聊（或群里 @ 机器人）发送类似消息即可触发：

> 帮我审核需求 https://xxx.feishu.cn/wiki/xxx 项目 demo

**第一步意图识别**会解析审核类型、项目与需求链接。审核类型规划为：需求审核、技术方案审核、代码CR、上线步骤审核；当前仅实现**需求审核**，其余类型识别后回复「暂未支持」，无关消息回复「不受理」。

**项目注册表** `projects.json`：意图识别匹配项目名称或别名后，取该项目配置的仓库地址（留空则跳过克隆）：

```json
{
  "projects": [
    {
      "name": "demo",
      "aliases": ["演示项目"],
      "code_repo": "git@github.com:org/repo.git",
      "know_base_repo": "git@github.com:org/knowbase.git"
    }
  ]
}
```

私有仓库依赖宿主机 git 凭证（ssh key / credential helper）。每次审核在 `workspaces/<项目>-<时间戳>/` 下建工作区，互不污染；deep agent 的工具根目录限定在该工作区内。

**输出**：审核报告写入 `workspace/result/review.md`，并通过飞书 API 创建一份正式的飞书文档（标题、分级标题、列表、粗体、行内代码均保留结构），机器人回复文档链接；创建失败兜底为分段回复全文。

### 已知限制

- 需求文档拉取用 `raw_content`，仅得到纯文本，表格/画板等内容会缺失；
- 意图识别依赖智谱端点的 function calling（`with_structured_output`）；
- 报告文档以应用身份创建在应用云空间，用户默认不可见——需将文档分享给用户，或配置 `QR_REPORT_FOLDER_TOKEN` 指向共享目录。

## 新增一个 workflow

在 `workflows/` 下新建模块，实现 `build(ctx) -> Callable[[str], WorkflowResult]` 并用 `@register("your_key")` 登记，把模块导入加到 `workflows/__init__.py` 底部即可；`QR_WORKFLOW=your_key` 生效。投递（回复文本/创建报告文档）由 `main.py` 适配层统一处理，workflow 内不需要感知飞书。

## 部署

长连接模式下进程需要常驻。以 Linux + systemd 为例：

```ini
# /etc/systemd/system/mini-feishu-bot.service
[Unit]
Description=mini-feishu-bot
After=network-online.target

[Service]
User=bot                          # 建议专用低权限用户，见下方安全须知
WorkingDirectory=/opt/mini-feishu-bot
EnvironmentFile=/opt/mini-feishu-bot/.env
ExecStart=/opt/mini-feishu-bot/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mini-feishu-bot
journalctl -u mini-feishu-bot -f   # 看日志
```

更新代码后 `git pull && uv sync && sudo systemctl restart mini-feishu-bot` 即可。macOS 上临时跑也可以用 `tmux` / `nohup uv run --env-file .env main.py &`。

## ⚠️ 安全须知

Agent 拥有**在宿主机上执行任意 shell 命令和读写文件**的能力（`LocalShellBackend`），且继承了运行环境的环境变量。这意味着：

- 任何能给机器人发消息的飞书用户，都可能让它在你的机器上执行命令；
- 请只部署在**可控、可牺牲**的环境，使用专用低权限用户运行；
- 不要把生产密钥、生产环境变量暴露给该进程；
- `dev_review` 的 agent 工具根目录限定在本次审核工作区；`simple` 默认是用户主目录，可用 `QR_SIMPLE_ROOT` 收窄到指定目录。

## 项目结构

```
main.py              飞书长连接客户端：过滤、去重、后台执行、按结果投递
workflows/
  __init__.py        workflow 注册表（可插拔入口）
  base.py            WorkflowResult / WorkflowContext / final_answer
  simple_agent.py    SimpleAgentWorkflow（电脑助手）
  dev_review.py      DevProcessReviewWorkflow（LangGraph 审核管线）
feishu_docs.py       飞书文档 API：需求拉取、报告文档创建（markdown→docx blocks）
projects.json        项目注册表（名称/别名/代码仓库/知识库仓库）
tests/               pytest：审核图全链路（假模型/假拉取）、blocks 转换、simple 回归
.env.example         环境变量模板（.env 已被 gitignore，不会提交）
pyproject.toml / uv.lock   依赖定义与锁定
```
