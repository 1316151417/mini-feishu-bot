# mini-feishu-bot

一个极简的飞书机器人:你在飞书里发消息,它背后是一个由 **LangGraph + LangChain**(通过 [deepagents](https://github.com/langchain-ai/deepagents))驱动的 Agent,可以在运行它的机器上执行 shell 命令、读写文件,然后把结果直接回复到飞书。

一句话:把「电脑助手」装进飞书,私聊或群里 @ 它即可。

## 特性

- **零回调、零公网**:使用飞书官方 SDK 的 **WebSocket 长连接**接收事件,不需要公网 IP、域名和 HTTPS 回调地址,在任何能上网的机器上都能跑。
- **极简架构**:单进程、两个文件。`main.py` 负责收发消息,`workflow.py` 负责 Agent。
- **Agent 能力**:基于 deepagents(LangGraph 状态机 + LangChain 工具调用),自带 shell 执行和文件读写工具,由模型自主规划多步任务。
- **模型可换**:默认通过智谱 OpenAI 兼容接口调用 `GLM-5.3-Flash`,改环境变量即可换模型。
- **细节处理**:消息去重、群里只响应 @、长回复自动分段(3000 字/条)、执行异常兜底回复。

## 工作原理

```
飞书用户 ──(WebSocket 长连接)──> lark-oapi 事件
    │
    ▼
main.py  过滤(仅文本、去重、群里需 @机器人)
    │  ThreadPoolExecutor 后台执行,不阻塞事件循环
    ▼
workflow.py  deepagents Agent(LangGraph)
    │  工具:execute(shell)、read_file、write_file …
    │  根目录:运行用户的主目录
    ▼
ChatOpenAI(智谱 GLM)── 多轮规划与工具调用
    │
    ▼
最终回答 ──> 回复原消息(自动分段)
```

## 快速开始

### 前置要求

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)(包管理器)
- 一个飞书自建应用,以及智谱 API Key

### 1. 创建飞书应用

1. 打开[飞书开放平台](https://open.feishu.cn/) → 创建企业自建应用,记下 **App ID** 和 **App Secret**。
2. 「添加应用能力」→ 开启**机器人**。
3. 「事件与回调」→ 订阅方式选择**长连接**(不要选 webhook),添加事件 `im.message.receive_v1`(接收消息)。
4. 「权限管理」→ 开通**获取与发送单聊、群组消息**(`im:message`)权限。
5. 发布应用版本并等待管理员通过(个人测试空间可自审自批)。

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`:

```dotenv
QR_FEISHU_APP_ID=cli_xxx            # 飞书应用 App ID
QR_FEISHU_APP_SECRET=your_secret    # 飞书应用 App Secret
ZHIPU_API_KEY=your_api_key          # 智谱 API Key
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4
QR_MODEL_NAME=GLM-5.3-Flash         # 可换成其他 GLM 模型
```

### 3. 安装依赖并运行

```bash
uv sync                            # 按 uv.lock 安装依赖到 .venv
uv run --env-file .env main.py
```

看到 `starting bot with open_id=...` 即启动成功。私聊机器人发一句「看看我的桌面有哪些文件」试试;拉进群后需要 **@机器人** 才会响应。

## 部署

长连接模式下进程需要常驻。以 Linux + systemd 为例:

```ini
# /etc/systemd/system/mini-feishu-bot.service
[Unit]
Description=mini-feishu-bot
After=network-online.target

[Service]
User=bot                          # 建议专用低权限用户,见下方安全须知
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

Agent 拥有**在宿主机上执行任意 shell 命令和读写主目录文件**的能力(`LocalShellBackend`),且继承了运行环境的环境变量。这意味着:

- 任何能给机器人发消息的飞书用户,都可能让它在你的机器上执行命令;
- 请只部署在**可控、可牺牲**的环境,使用专用低权限用户运行;
- 不要把生产密钥、生产环境变量暴露给该进程;
- 如需缩小范围,`workflow.py` 的 `build_workflow(model, workspace_root=...)` 支持把工具根目录限制到指定目录。

## 项目结构

```
main.py        飞书长连接客户端:过滤、去重、后台执行、回复
workflow.py    deepagents Agent 构建(LangGraph + LangChain)
.env.example   环境变量模板(.env 已被 gitignore,不会提交)
pyproject.toml / uv.lock   依赖定义与锁定
```
