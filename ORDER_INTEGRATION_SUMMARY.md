# 订单接入改造总结

## 背景

本次改造以 `C:\Users\11752\Downloads\接口契约.md` 和 `C:\Users\11752\Downloads\webtool_session_prompt.md` 为准，废弃旧订单 schema。

旧逻辑里的 `customer/product/quantity/orders.json` 不再作为订单对接结构使用。机器人现在负责把 Excel、照片、文字加单统一整理成 Web 工具要求的馄饨侯订单 JSON，并通过 HTTP 接口给 Web 工具拉取。

## 核心改动

### 1. 订单数据改为 SQLite

新增 SQLite 持久化，默认文件：

```text
orders.db
```

配置项：

```env
ORDER_DB_FILE=orders.db
```

订单表保存完整契约 JSON，同时冗余保存 `kind/source/store/status/confirmed/raw_ref/created_at`，用于查询和去重。

### 1.1 接口鉴权

所有 `/api/*` 接口都必须带：

```http
Authorization: Bearer <ROBOT_API_TOKEN>
```

不匹配或未配置 `ROBOT_API_TOKEN` 时返回 `401`。真实 token 只放在云端 `.env` 和 Web 工具 `.env`，机器人和 Web 工具必须使用同一个。

### 2. 新订单 JSON 格式

基础订单：

```json
{
  "kind": "base",
  "source": "excel",
  "store": "鼓楼店",
  "order_no": "10385",
  "orderer": "周凯",
  "order_date": "2026-06-20",
  "deliver_date": "2026-06-21",
  "items": [
    {
      "code": "05020093",
      "name": "鸡汤鲜肉馄饨",
      "spec": "260g/袋*25袋",
      "unit": "箱",
      "qty": 2,
      "price": 267.32,
      "category": "馄饨"
    }
  ],
  "confirmed": true,
  "status": "new",
  "raw_ref": "原文件名/原图URL/消息ID",
  "created_at": "2026-06-20T17:26:00"
}
```

文字补丁：

```json
{
  "kind": "patch",
  "source": "text",
  "store": "老三家",
  "order_date": "2026-06-21",
  "items": [
    {
      "code": null,
      "name": "鸡腿",
      "spec": null,
      "unit": "件",
      "qty": 20
    }
  ],
  "change_type": "add",
  "confirmed": true,
  "status": "new",
  "raw_text": "老三家鸡腿加20件",
  "raw_ref": "群名/消息ID",
  "created_at": "..."
}
```

## 三种来源处理

### 标准 Excel

标准 Excel 会解析成：

```text
kind=base
source=excel
confirmed=true
status=new
```

Excel 视为天然已确认，解析成功后直接写入 SQLite。

### 照片

照片识别使用可配置的 OpenAI-compatible 视觉模型。当前默认模型：

```text
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3-vl-plus
```

云上需要配置：

```env
VISION_API_KEY=your_qwen_or_dashscope_api_key
```

配置后，照片会解析成 `kind=base/source=photo` 草稿，微信用户确认后写入 SQLite。未配置时，机器人会提示视觉模型还没配置好。

### 文字加单

文字加单会解析成：

```text
kind=patch
source=text
confirmed=false
status=new
```

机器人只负责问清楚：

- 哪个门店
- 加什么/改什么
- 数量多少

不在机器人侧挂靠到某张基础订单。挂靠逻辑交给 Web 工具处理。

用户回复 `确认` 后，文字补丁写入 SQLite。

## Web 工具接口

### 拉取待处理订单

```http
GET /api/orders?status=new&order_date=2026-06-21
```

只返回：

```text
confirmed=true
status=new
order_date=2026-06-21
```

响应：

```json
{
  "orders": []
}
```

### 标记已拉取

```http
POST /api/orders/mark_fetched
Content-Type: application/json

{
  "ids": [123, 456]
}
```

成功后这些订单会变成：

```text
status=fetched
```

重复调用是幂等的。

返回：

```json
{
  "succeeded": [123],
  "failed": [456]
}
```

### 退回为未拉取

```http
POST /api/orders/unmark
Content-Type: application/json

{
  "ids": [123, 456]
}
```

成功后这些订单会回到：

```text
status=new
```

重复调用是幂等的；已经是 `new` 的 id 也算 `succeeded`，不存在或已取消的 id 算 `failed`。

### 重拉 / 查询

```http
GET /api/orders?status=fetched&order_date=2026-06-21
GET /api/orders?ids=123,456
GET /api/orders?status=all&order_date=2026-06-21
```

`status=all` 只忽略 `new/fetched` 状态，不忽略 `confirmed`，用于发货模块读取某个下单日期的全部已确认订单。

### 拉取产成品入库

```http
GET /api/receipts?date=2026-06-21
GET /api/receipts?status=fetched&date=2026-06-21
GET /api/receipts?status=all&date=2026-06-21
```

读取独立的 `receipts.db`，不读订单库。默认 `status=new`，只返回还没被 Web 工具标记已拉取的入库数据；`status=fetched` 可重拉已标记数据；`status=all` 返回未取消数据。

返回：

```json
{
  "receipts": [
    {
      "id": "r001",
      "date": "2026-06-21",
      "items": [
        {
          "code": null,
          "name": "鸡汤虾肉馄饨",
          "spec": null,
          "unit": "箱",
          "qty": 50
        }
      ]
    }
  ]
}
```

入库数据不带 `store`。

### 标记 / 退回产成品入库

```http
POST /api/receipts/mark_fetched
POST /api/receipts/unmark
Content-Type: application/json

{
  "ids": ["r001", "r002"]
}
```

`mark_fetched` 会把入库记录标记为已拉取，默认 `/api/receipts?date=...` 不再返回这些记录。`unmark` 会退回为未拉取，方便 Web 工具作废本批后重新同步。返回格式与订单一致：

```json
{
  "succeeded": ["r001"],
  "failed": ["r002"]
}
```

## 调试导入接口

这些接口用于本地联调或后续后台工具，不是 Web 工具必需接口。

```http
POST /api/orders/import/excel
POST /api/orders/import/photo
POST /api/orders/import/text
```

其中：

- Excel 导入直接入库。
- Photo 导入默认只生成草稿，`confirm=true` 时才入库。
- Text 导入默认只生成草稿，`confirm=true` 时才入库。

## 微信侧改动

企业微信客服消息处理新增：

- `text`：走文字加单/订单模式逻辑。
- `image`：订单模式下识别订单照片；入库模式下识别产成品入库照片；都先发用户确认。
- `file`：如果是 Excel，解析并直接入库；其他文件提示暂不支持。

新增微信命令：

```text
入库
```

入库模式确认后写入 `receipts.db`，不写 `orders.db`。

智能机器人普通回调仍以文字为主。

## 导出改动

`/exports/orders.xlsx` 现在从 SQLite 读取新订单 JSON 展平导出，不再读取 `orders.json`。

导出明细字段改为契约字段：

- ID
- 类型
- 来源
- 状态
- 已确认
- 门店/区域
- 订单号
- 下单人
- 下单日期
- 送达日期
- 变更类型
- 商品编码
- 商品名称
- 规格
- 单位
- 数量
- 单价
- 分类
- 原始文本
- 原始引用
- 创建时间

## 修改文件

```text
main.py
.env.example
requirements.txt
README.md
```

新增依赖：

```text
python-multipart
```

## 已验证

已做以下验证：

```powershell
python -m py_compile main.py
```

并用 FastAPI `TestClient` 验证：

- `GET /api/orders?status=new&order_date=YYYY-MM-DD`
- `POST /api/orders/mark_fetched`
- `POST /api/orders/unmark`
- `GET /api/orders?status=fetched&order_date=YYYY-MM-DD`
- `GET /api/receipts?status=fetched&date=YYYY-MM-DD`
- `POST /api/receipts/mark_fetched`
- `POST /api/receipts/unmark`

验证结果：

- `status=new` 可拉到新订单。
- 调 `mark_fetched` 后，`status=new` 不再返回该订单。
- `status=fetched` 可重拉已拉取订单。
- 调 `unmark` 后，订单或入库数据会重新进入默认待同步列表。

还验证了 Excel 解析后可写入 SQLite，并符合新 JSON 结构。

## 注意事项

- Web 工具不要直接连机器人 SQLite，只通过 `/api/orders`、`/api/orders/mark_fetched`、`/api/orders/unmark`、`/api/receipts` 和入库 mark/unmark 接口。
- Web 工具必须先成功并入汇总，再调用 `mark_fetched`。
- 照片识别调用 `VISION_MODEL`；未配置 `VISION_API_KEY` 时不会入库，会提示视觉模型还没配置好。
- Excel 标准表必须至少能识别出商品名称和数量列。
- 文字补丁不带挂靠订单信息，挂靠由 Web 工具按 `store` 和现有订单处理。
- `order_date` 是下单日期/归属日期，Web 工具按它分批。文字里出现 `6.21订`、`6月21日订`、`2026-6-21下单` 时优先写入 `order_date`。
- `deliver_date` 只是送达/到货备注，不参与分批；消息收到/入库时间写入 `created_at`，三者不能混用。
- `items[].qty` 入库前统一清洗成数字，单位放在 `unit`。
- confirmed 的 patch 必须有 `store`，否则拒绝入库。
- 产成品入库数据写入独立 `receipts.db`，GET `/api/receipts` 不返回 `store`，生成入库单成功后由 Web 工具调用入库 `mark_fetched`。
