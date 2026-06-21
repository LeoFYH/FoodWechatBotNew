# 机器人部署说明

## 运行拓扑

```text
Web 工具 -> 127.0.0.1:9000 FastAPI
```

FastAPI 只监听本机地址，不直接暴露公网。

## 环境变量

机器人和 Web 工具必须使用同一个 `ROBOT_API_TOKEN`。

本次生成的 token：

```text
<ROBOT_API_TOKEN>
```

机器人 `.env` 至少配置：

```env
LLM_API_KEY=your_deepseek_api_key
LLM_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat

ROBOT_API_TOKEN=<ROBOT_API_TOKEN>
ORDER_DB_FILE=orders.db
RECEIPT_DB_FILE=receipts.db

# Order photos and receipt photos use an OpenAI-compatible vision model.
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-plus
VISION_API_KEY=your_qwen_or_dashscope_api_key
```

Web 工具请求机器人 `/api/*` 时加请求头：

```http
Authorization: Bearer <ROBOT_API_TOKEN>
```

## 安装依赖

```bash
cd /path/to/FoodWechatBot
python -m pip install -r requirements.txt
```

## 启动 FastAPI

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 9000
```

生产环境建议用 systemd 或 supervisor 托管这个命令，保证进程退出后自动拉起。

## Nginx 配置

仓库内配置文件：

```text
deploy/nginx-foodwechatbot.conf
```

复制到 Nginx 配置目录：

```bash
sudo cp deploy/nginx-foodwechatbot.conf /etc/nginx/conf.d/foodwechatbot.conf
```

检查配置：

```bash
sudo nginx -t
```

重载 Nginx：

```bash
sudo systemctl reload nginx
```

HTTPS/证书部分由用户自行补充。

## 接口鉴权测试

未带 token 应返回 `401`：

```bash
curl -i "http://127.0.0.1:9000/api/orders?status=new&order_date=2026-06-21"
```

带 token 才能访问：

```bash
curl -H "Authorization: Bearer <ROBOT_API_TOKEN>" \
  "http://127.0.0.1:9000/api/orders?status=new&order_date=2026-06-21"
```
