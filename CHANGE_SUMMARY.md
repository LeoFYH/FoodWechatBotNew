# FoodWechatBot 订单接入变更总结

## 1. 订单接口加锁

所有 `/api/*` 接口现在都要求 Bearer Token：

```http
Authorization: Bearer <ROBOT_API_TOKEN>
```

涉及接口：

```text
GET /api/orders
POST /api/orders/mark_fetched
POST /api/orders/import/excel
POST /api/orders/import/photo
POST /api/orders/import/text
GET /api/receipts
```

未带 token、token 错误、或服务端未配置 `ROBOT_API_TOKEN`，都会返回 `401`。

真实 token 只放在云端 `.env` 和 Web 工具 `.env`，机器人和 Web 工具必须使用同一个。

## 2. 照片识别改成 Qwen 路线

不再默认使用 `gpt-4o-mini`，照片识别走可配置的 OpenAI-compatible 视觉模型。

当前配置改为：

```env
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-plus
# VISION_API_KEY=your_qwen_or_dashscope_api_key
```

当前行为：

- 订单照片和入库照片都调用 `VISION_MODEL`。
- 如果没有配置 `VISION_API_KEY`，微信侧会提示视觉模型还没配置好。

云上配置视觉 key 后即可启用：

```env
VISION_API_KEY=your_real_key
```

## 3. qty 清洗成纯数字

订单入库前会统一清洗 `items[].qty`。

示例：

```text
20件 -> 20
2箱 -> 2
3.5袋 -> 3.5
```

单位继续放在 `unit` 字段里，Web 工具侧可以直接把 `qty` 当数字处理。

## 4. 订单日期字段对齐

现在严格区分三个含义：

```text
order_date = 下单日期 / 归属日期，Web 工具按它分批
deliver_date = 客户要求送达/到货描述，只是备注
created_at = 消息收到/订单入库时间
```

文字解析优先读取 `X.XX订`：

```text
6.21订：小鱼馄饨1箱 -> order_date=2026-06-21
6月21日订：小鱼馄饨1箱 -> order_date=2026-06-21
2026-6-21下单：小鱼馄饨1箱 -> order_date=2026-06-21
```

`deliver_date` 不参与分批。文字里出现相对送达描述时仍可转换成备注：

```text
明早送 / 明天送 -> 明天日期
后天送 -> 后天日期
今天送 -> 今天日期
```

不会把消息时间错放进 `deliver_date`，也不会用 `deliver_date` 替代 `order_date`。

## 5. patch 必须带 store

文字补丁 `kind=patch/source=text` 入库前必须有 `store`。

如果没有门店：

- 不允许 confirmed 订单入库。
- 微信侧会继续让用户补充门店。

机器人只负责问清：

```text
哪个门店
加什么 / 改什么
数量多少
送达时间，如有
```

不负责挂靠到哪张基础订单，挂靠仍由 Web 工具侧处理。

## 6. 部署配置

新增部署文档：

```text
DEPLOYMENT.md
```

新增 Nginx 配置：

```text
deploy/nginx-foodwechatbot.conf
```

部署拓扑：

```text
Web 工具 -> 127.0.0.1:9000 FastAPI
```

FastAPI 只监听本机：

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 9000
```

Nginx 反向代理到：

```text
http://127.0.0.1:9000
```

证书 / HTTPS 留给用户后续自行配置。

## 6.1 订单接口改造

`order_entries` 增加独立 `order_date` 列和索引：

```text
order_date TEXT NOT NULL DEFAULT ''
idx_order_entries_order_date
idx_order_entries_status_order_date
```

老数据启动时自动迁移：只从 `payload_json.order_date` 回填；没有 `order_date` 的老数据保持空字符串。

`/api/orders` 支持：

```text
GET /api/orders?status=new&order_date=YYYY-MM-DD
GET /api/orders?status=fetched&order_date=YYYY-MM-DD
GET /api/orders?status=all&order_date=YYYY-MM-DD
```

`status=all` 只忽略 new/fetched 状态，但仍然只返回 `confirmed=true` 且 `order_date` 匹配的订单。

`mark_fetched` 返回固定格式：

```json
{ "succeeded": [123], "failed": [456] }
```

已 fetched 的 id 重复调用仍算 `succeeded`；不存在的 id 算 `failed`。

## 6.2 入库模式和产成品入库库

新增微信命令：

```text
入库
```

入库模式链路：

```text
车间发产成品照片
-> 视觉模型识别成成品清单
-> 微信复述给车间确认
-> 用户发“确认”
-> 写入 receipts.db
```

入库库与订单库分开：

```text
ORDER_DB_FILE=orders.db
RECEIPT_DB_FILE=receipts.db
```

`receipt_entries` 字段：

```text
id
date
created_at
updated_at
payload_json
```

`/api/receipts`：

```text
GET /api/receipts?date=YYYY-MM-DD
```

返回：

```json
{ "receipts": [] }
```

入库数据不带 `store`，不按门店分组，不调用 `mark_fetched`。

## 7. 配置文件更新

`.env.example` 新增：

```env
ROBOT_API_TOKEN=replace_with_generated_token
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-plus
# VISION_API_KEY=your_qwen_or_dashscope_api_key
ORDER_DB_FILE=orders.db
RECEIPT_DB_FILE=receipts.db
```

`.gitignore` 新增忽略：

```text
orders.db
receipts.db
*.db-wal
*.db-shm
```

## 8. 验证结果

已验证：

```text
python -m py_compile main.py
```

接口行为验证：

```text
/api/orders 未带 token -> 401
/api/orders 带 token -> 200
mark_fetched 正常
```

订单字段验证：

```text
qty "20件" -> 20
raw_text "6.21订：小鱼馄饨1箱" -> order_date=2026-06-21
"明早送" -> deliver_date=2026-06-22，仅作备注
created_at 保持消息/入库时间
patch 无 store -> 拒绝入库
GET /api/orders?status=all&order_date=... 可返回已 fetched 订单
GET /api/receipts?date=... 返回独立产成品入库数据，不带 store
```

## 9. 修改文件清单

```text
main.py
.env.example
.gitignore
README.md
requirements.txt
ORDER_INTEGRATION_SUMMARY.md
DEPLOYMENT.md
deploy/nginx-foodwechatbot.conf
CHANGE_SUMMARY.md
```
