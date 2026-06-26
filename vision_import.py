"""vision_import.py —— 视觉/照片解析层(从 main.py 原样搬出)。

把订单照片交给视觉大模型识别成基础订单 payload。

铁律(e):OpenAI vision client 与 model 留在 main,本模块**只通过参数接收** client/model,
自身不持有 client、不读 env、不 import main。依赖的纯函数从叶子模块(order_normalize/llm_json)导入。

注意:产成品入库照片识别 llm_parse_receipt_photo 依赖入库领域归一化(normalize_receipt_payload),
该函数尚在 main、待 P6 receipt_logic 抽出,故 receipt 照片识别留到 P6 一并搬入,本阶段不在此模块。
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from llm_json import extract_json_object
from order_normalize import (
    ORDER_KIND_BASE,
    ORDER_SOURCE_PHOTO,
    ORDER_STATUS_NEW,
    normalize_order_payload,
    now_iso,
)


def image_data_uri(image_bytes: bytes, mime_type: str | None) -> str:
    mime = mime_type or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def ensure_vision_recognition_ready(client: Any) -> None:
    if client is None:
        raise RuntimeError("vision model is not configured")


def call_vision_json(client: Any, model: str, prompt: str, image_bytes: bytes, mime_type: str | None) -> dict[str, Any]:
    ensure_vision_recognition_ready(client)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你只输出可解析 JSON。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_uri(image_bytes, mime_type)}},
                ],
            },
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    return extract_json_object(raw)


def llm_parse_photo_order(client: Any, model: str, image_bytes: bytes, mime_type: str | None, raw_ref: str) -> dict[str, Any]:
    today = datetime.now().date().isoformat()
    current_year = datetime.now().year
    prompt = f"""
你是通用订单照片识别助手。请读取图片中的订单表格或手写订单，输出 Web 工具可直接使用的基础订单 JSON。

只输出一个 JSON 对象，不要解释，不要 Markdown。

格式：
{{
  "kind":"base",
  "source":"photo",
  "store":"门店/区域",
  "order_no":"有则填，没有留空",
  "orderer":"可空",
  "order_date":"YYYY-MM-DD 或原文，可空",
  "deliver_date":"YYYY-MM-DD 或原文，可空",
  "items":[
    {{"code":"商品编码或#N/A或空","name":"商品名称","spec":"规格","unit":"单位","qty":2,"price":267.32,"category":"分类"}}
  ],
  "confirmed":false,
  "status":"new",
  "raw_ref":"",
  "created_at":""
}}

要求：
- 今天日期：{today}，当前年份：{current_year}。
- order_date 是订单标题/表头里的下单日期或归属日期，例如“6.16下午订单”“6.16订”必须按当前年份输出为“{current_year}-06-16”。
- 表格中的送货/配送/到货日期只放 deliver_date，不要拿它替代 order_date。
- qty 和 price 尽量输出数字，识别不到用 null。
- code、spec、category 识别不到用空字符串。
- deliver_date 只填客户要求送达/到货日期；created_at 不要填送达日期。
- 不要编造图片里没有的信息。
- 多个商品拆成多个 items。
""".strip()

    parsed = call_vision_json(client, model, prompt, image_bytes, mime_type)
    parsed["kind"] = ORDER_KIND_BASE
    parsed["source"] = ORDER_SOURCE_PHOTO
    parsed["confirmed"] = False
    parsed["status"] = ORDER_STATUS_NEW
    parsed["raw_ref"] = raw_ref
    parsed["created_at"] = now_iso()
    return normalize_order_payload(parsed)


__all__ = [
    "image_data_uri",
    "ensure_vision_recognition_ready",
    "call_vision_json",
    "llm_parse_photo_order",
]
