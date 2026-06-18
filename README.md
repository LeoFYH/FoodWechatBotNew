# WechatClaw

微信 AI 机器人项目。当前代码处于 **Phase 1：部署准备 + 上云**。

当前阶段只做 AI backend 部署准备：

- FastAPI 后端
- OpenAI-compatible SDK 调用 DeepSeek
- `user_id` 级别的本地记忆
- `memory.json` 持久化
- Swagger `/docs` 测试
- Render 部署配置

暂时不做企业微信、webhook、PDF、数据库、Docker、前端、登录、支付、服务号。

## 安装

进入项目目录：

```powershell
cd D:\WechatClaw
```

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

如果 `python` 命令不存在，需要先安装 Python，并重新打开 PowerShell。

## 配置 .env

复制示例配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```powershell
notepad .env
```

DeepSeek 配置示例：

```env
LLM_API_KEY=your_deepseek_api_key
LLM_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
MAX_HISTORY_MESSAGES=20
SYSTEM_PROMPT=你是一个运行在微信里的 AI 助手，回答要简洁、有帮助。
```

`.env` 是本地密钥文件，不要提交到 Git。

## 运行

```powershell
python -m uvicorn main:app --reload
```

打开 Swagger：

```text
http://127.0.0.1:8000/docs
```

生产启动命令：

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 测试 /health

浏览器打开：

```text
http://127.0.0.1:8000/health
```

预期返回：

```json
{
  "status": "ok"
}
```

PowerShell 测试：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
```

## 测试 /chat

在 Swagger 里打开 `POST /chat`，点击 `Try it out`，输入：

```json
{
  "user_id": "leo",
  "message": "你好，我叫 Leo，我在开发 WechatClaw。"
}
```

再发一次：

```json
{
  "user_id": "leo",
  "message": "我是谁？我在做什么项目？"
}
```

预期：AI 能根据 `leo` 的历史记忆回答。

PowerShell 测试：

```powershell
$body = @{
  user_id = "leo"
  message = "你好，我叫 Leo，我在开发 WechatClaw。"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://127.0.0.1:8000/chat `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

## 测试多用户记忆隔离

先给 `leo` 写入记忆：

```json
{
  "user_id": "leo",
  "message": "我叫 Leo，我在开发 WechatClaw。"
}
```

再用另一个用户提问：

```json
{
  "user_id": "alice",
  "message": "我是谁？我在做什么项目？"
}
```

预期：`alice` 不应该知道 `leo` 的信息。

查看用户记忆长度：

```text
GET http://127.0.0.1:8000/memory/leo
GET http://127.0.0.1:8000/memory/alice
```

返回示例：

```json
{
  "user_id": "leo",
  "history_length": 4
}
```

## 清除用户记忆

Swagger 里调用：

```text
DELETE /memory/{user_id}
```

例如：

```text
DELETE http://127.0.0.1:8000/memory/leo
```

预期返回：

```json
{
  "deleted": true,
  "user_id": "leo"
}
```

PowerShell 测试：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/memory/leo -Method Delete
```

## 当前 API

### GET /health

健康检查。

### POST /chat

请求：

```json
{
  "user_id": "leo",
  "message": "你好"
}
```

返回：

```json
{
  "user_id": "leo",
  "answer": "你好！有什么我可以帮你的吗？",
  "history_length": 2
}
```

### GET /memory/{user_id}

查看某个用户的记忆长度。

### DELETE /memory/{user_id}

清除某个用户的记忆。

## Render 部署

项目包含 [render.yaml](<D:/WechatClaw/render.yaml>)，Render 可以按这个配置构建和启动服务。

配置内容：

```yaml
buildCommand: pip install -r requirements.txt
startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
```

`main.py` 中的 `app = FastAPI(...)` 可被 `uvicorn main:app` 调用。

### Render 环境变量

在 Render Dashboard 的 Environment 里配置：

```text
LLM_API_KEY=your_deepseek_api_key
LLM_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
MAX_HISTORY_MESSAGES=20
SYSTEM_PROMPT=你是一个运行在微信里的 AI 助手，回答要简洁、有帮助。
```

不要在 Render 或 Git 里提交本地 `.env` 文件。`.env` 只用于本地开发，已经被 `.gitignore` 排除。

### Render 部署步骤

1. 把代码推到 GitHub。
2. 在 Render 创建新的 Web Service，或使用 Blueprint 导入 `render.yaml`。
3. Build Command 使用：

```text
pip install -r requirements.txt
```

4. Start Command 使用：

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

5. 在 Environment 配好 `LLM_API_KEY`、`LLM_BASE_URL`、`MODEL_NAME`、`MAX_HISTORY_MESSAGES`、`SYSTEM_PROMPT`。
6. 部署完成后拿到公网 URL，例如：

```text
https://your-app.onrender.com
```

### memory.json 说明

`memory.json` 保留给本地开发使用。应用启动时如果文件不存在，会按空 memory 处理，并在第一次写入时创建。

Render 免费服务的文件系统不适合长期持久化：服务重启、重新部署或实例迁移后，本地文件里的记忆可能丢失。Phase 1 先接受这个限制，用来验证云端 API 链路；Phase 2 再把 memory 换成数据库。

`memory.json` 已加入 `.gitignore`，避免把本地测试对话提交到远端仓库。

## 云端测试

把下面的 `https://your-app.onrender.com` 换成你的 Render URL。

测试健康检查：

```bash
curl https://your-app.onrender.com/health
```

预期返回：

```json
{"status":"ok"}
```

第一次对话，写入记忆：

```bash
curl -X POST https://your-app.onrender.com/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"yuhang","message":"你好，记住我在做微信AI机器人"}'
```

第二次对话，测试记忆：

```bash
curl -X POST https://your-app.onrender.com/chat \
  -H "Content-Type: application/json" \
  -d '{"user_id":"yuhang","message":"我在做什么？"}'
```

查看记忆长度：

```bash
curl https://your-app.onrender.com/memory/yuhang
```

清除记忆：

```bash
curl -X DELETE https://your-app.onrender.com/memory/yuhang
```

## 项目方向

长期目标是微信 Bot 租用平台。用户在微信里付费，拿到一个属于自己的 Bot，通过企业微信或后续服务号能力跟 Bot 对话。

后续阶段才考虑：

- 企业微信消息接入
- PDF 总结
- 多用户正式存储
- 服务号
- 支付
- 上云部署

## 订单模式

同一个客服机器人现在支持显式切换模式：

- 发 `订单`：进入订单模式。
- 发 `订单：老三家 鸡腿 20件 明早送`：直接进入订单模式并整理订单草稿。
- 发 `确认`：确认当前订单草稿并写入 `orders.json`。
- 发 `取消`：清空当前订单草稿。
- 发 `问诊`：切回原来的需求访谈模式。
- 发 `导出订单`：返回订单 Excel 下载地址。

订单导出接口：

```text
GET /exports/orders.xlsx
```

如果配置了 `EXPORT_TOKEN`，下载订单表也需要带同一个 `token` 参数。
