# 机器人部署说明

## 运行拓扑

```text
Web 工具 -> 127.0.0.1:9000 FastAPI
FastAPI -> Redis 缓存/操作流 -> PostgreSQL
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
LLM_API_KEY=your_qwen_or_dashscope_api_key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen3-vl-plus

ROBOT_API_TOKEN=<ROBOT_API_TOKEN>
ORDER_DB_FILE=orders.db
RECEIPT_DB_FILE=receipts.db

# 默认仍使用 SQLite 本地文件。切 PostgreSQL 时改为 postgres，并同时配置 Redis + DATABASE_URL。
DATABASE_BACKEND=sqlite
# DATABASE_URL=postgresql://user:password@127.0.0.1:5432/foodwechatbot
# DEFAULT_TENANT_CODE=default
# REDIS_URL=redis://127.0.0.1:6379/0
# REDIS_CACHE_ENABLED=true
# REDIS_KEY_PREFIX=foodwechatbot
# REDIS_CACHE_TTL_SECONDS=300
# REDIS_OPERATION_STREAM=foodwechatbot:default:storage:events
# REDIS_STREAM_MAXLEN=10000

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

## PostgreSQL 切换流程

默认存储仍是 SQLite。要切 PostgreSQL，必须先准备 Redis 和 PostgreSQL。PG 后端不允许绕过 Redis 直接读写；所有 `models` 写操作都会先写入 Redis storage event，再同步落 PostgreSQL，并在 Redis 中维护查询缓存。

先在目标库执行建表脚本：

```bash
psql "$DATABASE_URL" -f migrations/postgres/0001_init.up.sql
```

把现有 SQLite 数据导入 PostgreSQL：

```bash
DATABASE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" \
  REDIS_URL="$REDIS_URL" \
  python3 scripts/migrate_sqlite_to_postgres.py
```

这个脚本会迁移：

- `orders.db` -> `orders` / `order_items`
- `receipts.db` -> `production_receipts` / `production_receipt_items`
- `memory.json` -> PG 会话状态表
- `session_state.json` -> PG 会话状态表
- `interviews.json` -> PG 会话状态表
- `kf_cursors.json` -> `channel_cursors`

确认 `/api/orders`、`/api/receipts` 与原 SQLite 返回一致后，再启动服务：

```bash
DATABASE_BACKEND=postgres DATABASE_URL="$DATABASE_URL" \
  REDIS_URL="$REDIS_URL" \
  python3 -m uvicorn main:app --host 127.0.0.1 --port 9000
```

迁移脚本会写入 `legacy_source/legacy_id`，重复执行不会重复导入同一条 SQLite 历史记录。Redis 里会保留 `REDIS_OPERATION_STREAM` 指定的 storage event 流，默认最多保留最近 10000 条事件；查询缓存默认 TTL 为 300 秒。

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
