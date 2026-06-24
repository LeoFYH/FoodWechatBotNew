from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable


INTENT_CONFIRM = "confirm"
INTENT_MODIFY = "modify"
INTENT_REJECT = "reject"
INTENT_CANCEL = "cancel"
INTENT_EXIT = "exit"
INTENT_CHAT = "chat"
INTENT_UNCLEAR = "unclear"

ALLOWED_INTENTS = {
    INTENT_CONFIRM,
    INTENT_MODIFY,
    INTENT_REJECT,
    INTENT_CANCEL,
    INTENT_EXIT,
    INTENT_CHAT,
    INTENT_UNCLEAR,
}

LLM_CONFIRM_THRESHOLD = 0.8
LLM_MODIFY_THRESHOLD = 0.75
LLM_REJECT_THRESHOLD = 0.75

LLMClassifier = Callable[[list[dict[str, str]]], str]


@dataclass(frozen=True)
class BusinessIntent:
    intent: str
    confidence: float
    source: str
    reason: str = ""

    @property
    def is_rule(self) -> bool:
        return self.source == "rule"


def normalize_reply(message: str) -> str:
    return re.sub(r"\s+", "", str(message or "").strip().lower())


def contains_any(text: str, keywords: set[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


EXACT_CANCEL = {"取消", "取消订单", "取消草稿", "清空", "清空订单", "清空草稿", "不要了"}
EXACT_EXIT = {"退出", "结束", "返回", "退出订单", "退出入库", "结束订单", "结束入库", "普通模式", "聊天"}

CANCEL_KEYWORDS = {"取消", "清空", "不要了", "作废", "删掉草稿"}
EXIT_KEYWORDS = {"退出", "返回普通", "结束订单", "结束入库"}
REJECT_KEYWORDS = {
    "不对",
    "不是",
    "错了",
    "有误",
    "不行",
    "不可以",
    "先别",
    "别",
    "不要",
    "等一下",
    "等等",
    "稍等",
    "等会",
}
MODIFY_KEYWORDS = {
    "改",
    "修改",
    "改成",
    "换成",
    "调整",
    "重发",
    "重新发",
    "补充",
    "数量是",
    "门店是",
    "日期是",
}
CONFIRM_EXACT = {
    "确认",
    "确认无误",
    "保存",
    "提交",
    "可以",
    "可以的",
    "行",
    "行的",
    "好",
    "好的",
    "好嘞",
    "对",
    "对的",
    "是",
    "是的",
    "没错",
    "没问题",
    "嗯",
    "嗯嗯",
    "ok",
    "okay",
    "yes",
    "y",
}
CONFIRM_STARTS = (
    "确认",
    "保存",
    "提交",
    "对",
    "是",
    "好",
    "行",
    "可以",
    "没错",
    "没问题",
    "ok",
    "okay",
    "yes",
    "y",
    "妥",
    "安排",
    "走吧",
    "就这样",
)
QUESTION_LIKE_KEYWORDS = {"吗", "么", "?", "？", "能不能", "可不可以", "是否", "怎么", "如何", "什么", "多少", "价格", "发票"}


def looks_like_correction(text: str) -> bool:
    if contains_any(text, MODIFY_KEYWORDS):
        return True
    if contains_any(text, {"不对", "不是", "错了", "有误"}) and re.search(r"\d|箱|件|袋|盒|包|斤|公斤|kg|克|门店|日期|下单|送", text):
        return True
    return False


def looks_like_question(text: str) -> bool:
    return contains_any(text, QUESTION_LIKE_KEYWORDS)


def classify_by_rules(message: str, *, has_draft: bool) -> BusinessIntent:
    text = normalize_reply(message)
    if not text:
        return BusinessIntent(INTENT_UNCLEAR, 0.0, "rule", "empty")

    if text in EXACT_CANCEL or contains_any(text, CANCEL_KEYWORDS):
        return BusinessIntent(INTENT_CANCEL, 0.98, "rule", "cancel keyword")
    if text in EXACT_EXIT or contains_any(text, EXIT_KEYWORDS):
        return BusinessIntent(INTENT_EXIT, 0.98, "rule", "exit keyword")

    if not has_draft:
        return BusinessIntent(INTENT_CHAT, 0.7, "rule", "no draft")

    if looks_like_correction(text):
        return BusinessIntent(INTENT_MODIFY, 0.92, "rule", "correction keyword")
    if contains_any(text, REJECT_KEYWORDS):
        return BusinessIntent(INTENT_REJECT, 0.92, "rule", "reject keyword")
    if not looks_like_question(text) and (text in CONFIRM_EXACT or text.startswith(CONFIRM_STARTS)):
        return BusinessIntent(INTENT_CONFIRM, 0.95, "rule", "confirm phrase")

    return BusinessIntent(INTENT_UNCLEAR, 0.0, "rule", "needs llm")


def build_intent_messages(message: str, *, mode: str, draft_summary: str) -> list[dict[str, str]]:
    clipped_summary = str(draft_summary or "")[:1600]
    return [
        {
            "role": "system",
            "content": (
                "你只做业务回复意图分类，不执行任何业务动作。"
                "用户刚收到一份待确认的订单或入库草稿。"
                "判断用户这句话相对上一份草稿的意图。"
                "只能输出 JSON，格式为 {\"intent\":\"...\",\"confidence\":0.0,\"reason\":\"...\"}。"
                "intent 只能是 confirm、modify、reject、cancel、exit、chat、unclear。"
                "confirm=同意保存/入库/继续/让系统赶紧处理；"
                "modify=给出修改内容或要求改某字段；"
                "reject=表示不对但未给明确修改；"
                "cancel=明确取消或清空草稿；"
                "exit=明确退出业务模式；"
                "chat=普通聊天；unclear=不确定。"
                "不要输出解释文本，不要输出 Markdown。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"业务模式：{mode or 'unknown'}\n"
                f"待确认草稿摘要：\n{clipped_summary}\n\n"
                f"用户回复：{message}"
            ),
        },
    ]


def extract_json_object(text: str) -> dict:
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


def parse_llm_intent(raw_text: str) -> BusinessIntent:
    data = extract_json_object(raw_text)
    intent = str(data.get("intent") or "").strip().lower()
    if intent not in ALLOWED_INTENTS:
        return BusinessIntent(INTENT_UNCLEAR, 0.0, "llm", "invalid intent")
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "")[:160]
    return BusinessIntent(intent, confidence, "llm", reason)


def classify_business_intent(
    message: str,
    *,
    has_draft: bool,
    mode: str,
    draft_summary: str = "",
    llm_classifier: LLMClassifier | None = None,
) -> BusinessIntent:
    rule_intent = classify_by_rules(message, has_draft=has_draft)
    if rule_intent.intent != INTENT_UNCLEAR or not has_draft or llm_classifier is None:
        return rule_intent

    try:
        raw_result = llm_classifier(build_intent_messages(message, mode=mode, draft_summary=draft_summary))
    except Exception:
        return BusinessIntent(INTENT_UNCLEAR, 0.0, "llm_error", "classifier failed")

    llm_intent = parse_llm_intent(raw_result)
    if llm_intent.intent == INTENT_CONFIRM and llm_intent.confidence >= LLM_CONFIRM_THRESHOLD:
        return llm_intent
    if llm_intent.intent == INTENT_MODIFY and llm_intent.confidence >= LLM_MODIFY_THRESHOLD:
        return llm_intent
    if llm_intent.intent == INTENT_REJECT and llm_intent.confidence >= LLM_REJECT_THRESHOLD:
        return llm_intent
    # Cancel and exit clear state, so they are only actionable from deterministic rules.
    if llm_intent.intent == INTENT_CHAT and llm_intent.confidence >= 0.85:
        return llm_intent
    return BusinessIntent(INTENT_UNCLEAR, llm_intent.confidence, llm_intent.source, llm_intent.reason)
