"""
agent_router.py —— 消息分诊"大脑"（前门路由层）

职责单一：收到一条自然语言客服消息，判断它该交给哪个业务处理器，
并尽量顺手抽取关键字段。它只"判断"，绝不执行任何业务动作
（不写库、不改草稿、不发消息）。

设计原则（对应"有边界的 agent"）：
1. 先走便宜的规则快路（在 main.py 里），命中就返回，不调用大模型。
2. 规则拿不准时，调用一次大模型做"结构化分诊 + 轻量抽取"。
   关键：这一步【不再被关键词限制】，所以自然语言/复杂订单消息也能被识别，
   这正是旧关键词路由 should_call_global_business_route_llm 卡死的痛点。
3. 大模型只输出 JSON；置信度不够就返回 unclear，交回旧逻辑兜底。
4. 危险动作（确认/取消/退出/撤回/写库）不由本模块决定，
   调用方用确定性规则处理。大模型提议，代码裁决。

本模块只依赖标准库，不 import main，避免循环依赖；大模型调用通过
`llm_classifier` 注入（与 services/business_intent.py 的约定一致）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# 路由意图：字符串值必须与 main.py 的 GLOBAL_ROUTE_* 完全一致，
# 这样 handle_user_message 现有的分发逻辑无需改动即可直接复用。
ROUTE_CHAT = "chat"
ROUTE_ORDER_TEXT = "order_text"
ROUTE_ENTER_ORDER = "enter_order"
ROUTE_ENTER_RECEIPT = "enter_receipt"
ROUTE_ORDER_QUERY = "order_query"
ROUTE_UNCLEAR = "unclear"

ALLOWED_ROUTES = {
    ROUTE_CHAT,
    ROUTE_ORDER_TEXT,
    ROUTE_ENTER_ORDER,
    ROUTE_ENTER_RECEIPT,
    ROUTE_ORDER_QUERY,
    ROUTE_UNCLEAR,
}

# 能触发业务动作的路由，需达到较高置信度才采信。
ACTIONABLE_ROUTES = {
    ROUTE_ORDER_TEXT,
    ROUTE_ENTER_ORDER,
    ROUTE_ENTER_RECEIPT,
    ROUTE_ORDER_QUERY,
}

# 置信度阈值（沿用原 classify_global_business_route 的取值，行为一致）。
ACTIONABLE_CONFIDENCE_THRESHOLD = 0.78
CHAT_CONFIDENCE_THRESHOLD = 0.85

# 注入式大模型分类器：输入 messages，返回模型原始文本（通常是一段 JSON）。
LLMClassifier = Callable[[list[dict[str, str]]], str]


@dataclass(frozen=True)
class RouterDecision:
    """前门路由的一次判断结果。"""

    intent: str                 # ROUTE_* 之一
    confidence: float           # 0.0 ~ 1.0
    source: str                 # "rule" | "llm" | "llm_error"
    reason: str = ""            # 便于排查：为什么这么判
    fields: dict[str, Any] = field(default_factory=dict)  # 轻量抽取（可空），下游可选用

    @property
    def is_actionable(self) -> bool:
        return self.intent in ACTIONABLE_ROUTES


def build_route_messages(message: str, *, mode: str = "") -> list[dict[str, str]]:
    """构造给大模型的分诊提示词：意图菜单 + 轻量抽取，只允许输出 JSON。"""
    mode_line = f"\n当前会话模式：{mode or 'unknown'}" if mode else ""
    return [
        {
            "role": "system",
            "content": (
                "你是餐饮微信客服的消息分诊器。你只判断这条消息应该走哪条业务路由，"
                "并尽量抽取关键信息，绝不执行任何业务动作。\n"
                "只能输出 JSON，格式："
                "{\"route\":\"...\",\"confidence\":0.0,\"reason\":\"...\","
                "\"fields\":{}}。\n"
                "route 只能是以下之一：\n"
                "- order_text：用户直接给了订单内容（门店/商品/数量/日期等，哪怕表达很口语、很复杂）。\n"
                "- enter_order：用户明确想进入录单/下单，但还没给订单明细。\n"
                "- enter_receipt：用户要记录产成品/车间入库。\n"
                "- order_query：用户查询订单库 / 同步 / 拉取结果。\n"
                "- chat：普通客服闲聊或业务咨询。\n"
                "- unclear：你无法确定。\n"
                "fields 是可选的轻量抽取，命中 order_text 时尽量填，例如："
                "{\"store\":\"鼓楼店\",\"deliver_date\":\"明天\","
                "\"items\":[{\"name\":\"鲜肉馄饨\",\"quantity\":20}]}；拿不准就留空 {}。\n"
                "重要：确认、取消、退出、撤回这些动作不归你决定，"
                "遇到这类消息只输出 route 为 chat 或 unclear。\n"
                "不要输出解释文本，不要输出 Markdown，只输出 JSON。"
            ),
        },
        {"role": "user", "content": f"{message}{mode_line}"},
    ]


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里稳健地解析出第一个 JSON 对象，失败返回 {}。"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def parse_route_decision(raw_text: str) -> RouterDecision:
    """把大模型的原始输出解析成 RouterDecision，并做校验/夹取。"""
    data = _extract_json_object(raw_text)
    route = str(data.get("route") or data.get("intent") or "").strip().lower()
    if route not in ALLOWED_ROUTES:
        return RouterDecision(ROUTE_UNCLEAR, 0.0, "llm", "invalid route")

    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}

    reason = str(data.get("reason") or "")[:160]
    return RouterDecision(route, confidence, "llm", reason, fields)


def decide_from_llm(
    message: str,
    *,
    mode: str = "",
    llm_classifier: LLMClassifier,
    min_confidence: float = ACTIONABLE_CONFIDENCE_THRESHOLD,
) -> RouterDecision:
    """
    大脑的大模型分诊步骤（规则拿不准时由 main.py 调用）。

    - llm_classifier: 注入的大模型调用，输入 messages、返回原始文本。
    - 返回置信度不达标的判断会被压成 unclear，让调用方安全兜底。
    """
    try:
        raw = llm_classifier(build_route_messages(message, mode=mode))
    except Exception:
        # 大模型调用失败：不要瞎猜，交回旧逻辑兜底。
        return RouterDecision(ROUTE_UNCLEAR, 0.0, "llm_error", "router llm failed")

    decision = parse_route_decision(raw)

    if decision.is_actionable and decision.confidence >= min_confidence:
        return decision
    if decision.intent == ROUTE_CHAT and decision.confidence >= CHAT_CONFIDENCE_THRESHOLD:
        return decision

    # 置信度不够：保留原因/抽取，但意图压成 unclear，由调用方决定兜底走法。
    return RouterDecision(ROUTE_UNCLEAR, decision.confidence, decision.source, decision.reason, decision.fields)
