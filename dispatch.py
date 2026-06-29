"""dispatch.py —— 消息分发与处理逻辑(从 main.py 逐字搬出,P10 步骤1:纯机械抽取)。

handle_user_message 分发主干 + 各业务 handler + LLM 封装/分诊 + handler 私有回复助手。
**业务逻辑、分发顺序、危险动作硬判一字未改**(命令拦截块仍在最前,is_exit/取消/撤回在前、
is_confirm 在 has_draft 分发后,全部仍在 agent_router/classify_global_business_route 之前)。

会话/草稿状态、数据双轨分诊器、wecom 簇、config/clients/env 留在 main,经 from main import 引用。
铁律(reload):测试 setUp 须同时 pop 本模块,使重载后重新 from main import 绑定到新 main(状态正确)。
被测试 patch 的 handle_order_user_message/call_customer_chat_llm/call_business_intent_llm/
llm_*_from_message 随调用方搬入此处,测试 patch 目标重定向到 dispatch.X。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import datetime
from typing import Any

from fastapi import HTTPException

import agent_router
from main import (
    BUSINESS_NEGATION_KEYWORDS,
    CHAT_SKILL_FILE,
    HELP_SKILL_FILE,
    ORDER_STATUS_FETCHED,
    BusinessIntent,
    CUSTOMER_CHAT_PROMPT,
    ChatResponse,
    clear_pending_confirm,
    clear_pending_revoke,
    get_pending_confirm,
    get_pending_revoke,
    query_order_payloads,
    set_pending_confirm,
    set_pending_revoke,
    summarize_order_for_reply,
    GLOBAL_ROUTE_CHAT,
    GLOBAL_ROUTE_ENTER_ORDER,
    GLOBAL_ROUTE_ENTER_RECEIPT,
    GLOBAL_ROUTE_ORDER_QUERY,
    GLOBAL_ROUTE_ORDER_TEXT,
    INTENT_CANCEL,
    INTENT_CHAT,
    INTENT_CONFIRM,
    INTENT_EXIT,
    INTENT_MODIFY,
    INTENT_REJECT,
    INTENT_UNCLEAR,
    MAX_HISTORY_MESSAGES,
    MEMORY_LOCK,
    MODEL_NAME,
    ORDER_CANCEL_COMMANDS,
    ORDER_EXPORT_COMMANDS,
    ORDER_KIND_BASE,
    ORDER_KIND_PATCH,
    ORDER_SOURCE_TEXT,
    ORDER_SKILL_FILE,
    ORDER_STATUS_NEW,
    PROMPT_TURN_CONTEXT,
    RECEIPT_SKILL_FILE,
    SESSION_MODE_CHAT,
    SESSION_MODE_ORDER,
    SESSION_MODE_RECEIPT,
    SYSTEM_PROMPT,
    VISION_MODEL,
    build_mode_help_message,
    build_order_export_message,
    build_order_storage_query_reply,
    build_status_message,
    business_mode_switch_blocked_reply,
    calibrate_receipt_items,
    cancel_latest_order_for_user,
    cancel_latest_receipt_for_user,
    classify_business_intent,
    clear_order_draft,
    clear_receipt_draft,
    client,
    command_contains_any,
    excel_file_signature,
    exit_business_mode,
    extract_explicit_order_date,
    extract_json_object,
    format_order_draft_summary,
    get_order_draft,
    get_receipt_draft,
    get_session_mode,
    get_session_record,
    has_raw_order_draft,
    has_raw_receipt_draft,
    insert_order_payload,
    is_confirm_command,
    is_exit_mode_command,
    is_mode_help_command,
    is_order_draft_view_command,
    is_order_mode_command,
    is_order_storage_query_command,
    is_receipt_mode_command,
    is_receipt_revoke_target,
    is_revoke_command,
    is_status_command,
    llm_parse_photo_order,
    llm_parse_receipt_photo,
    load_memory,
    logger,
    looks_like_order_message,
    missing_fields_reply,
    models,
    normalize_command,
    normalize_order_draft,
    normalize_receipt_payload,
    now_iso,
    order_draft_has_content,
    order_draft_missing_fields,
    parse_excel_order_payloads,
    receipt_draft_has_content,
    receipt_missing_fields,
    save_confirmed_order,
    save_confirmed_receipt,
    save_memory,
    save_order_draft,
    save_receipt_draft,
    save_session_record,
    try_switch_business_mode,
    user_order_count,
    vision_client,
)


# ====== 重复用户回复模板抽常量(P10 步骤2，文案逐字不变) ======
ENTER_ORDER_MODE_REPLY = "好的，进入订单模式了，直接发订单文字、Excel 或照片都行，发“退出”就退出。"
ENTER_RECEIPT_MODE_REPLY = "好的，进入入库模式了，发产成品入库照片就行，发“退出”就退出。"
HUMAN_TRANSFER_REPLY = "这个我不瞎承诺，我帮您转人工处理。"
NO_ORDER_DRAFT_REPLY = "现在没有待确认的订单草稿。直接发订单文字、Excel 或照片都行。"
ORDER_DRAFT_CLEARED_REPLY = "已清空当前订单草稿，并回到普通聊天。要继续录单再发“订单”。"
NO_RECEIPT_DRAFT_REPLY = "现在没有待确认的入库草稿。发产成品入库照片给我就行。"
RECEIPT_DRAFT_CLEARED_REPLY = "已清空当前入库草稿，并回到普通聊天。要继续入库再发“入库”。"
CONFIRM_HINT_MODIFY = "\n确认无误请回复“确认 / 对 / ok / yes”；不要这单请回“取消”；要修改就直接发修改内容。"
CONFIRM_HINT_CONTINUE_MODIFY = "\n确认无误请回复“确认 / 对 / ok / yes”；不要这单请回“取消”；要继续修改就直接发修改内容。"


def call_global_business_route_llm(messages: list[dict[str, str]]) -> str:
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()

def classify_global_business_route(message: str) -> BusinessIntent:
    command = normalize_command(message)
    if is_order_storage_query_command(command):
        return BusinessIntent(GLOBAL_ROUTE_ORDER_QUERY, 0.95, "rule", "order storage query")
    if command_contains_any(command, BUSINESS_NEGATION_KEYWORDS):
        return BusinessIntent(GLOBAL_ROUTE_CHAT, 0.75, "rule", "business negation")
    if is_order_mode_command(command):
        return BusinessIntent(GLOBAL_ROUTE_ENTER_ORDER, 0.92, "rule", "order mode command")
    if is_receipt_mode_command(command):
        return BusinessIntent(GLOBAL_ROUTE_ENTER_RECEIPT, 0.92, "rule", "receipt mode command")
    # 规则拿不准 → 交给 agent_router 大脑做大模型分诊。
    # 注意：这里【不再有关键词闸门 should_call_global_business_route_llm】，
    # 所以自然语言 / 复杂订单消息也会进大模型分诊，而不是被默认当成聊天丢掉——
    # 这正是修复"复杂消息看不懂"的关键改动。置信度阈值已内置在 agent_router 中（0.78 / 0.85）。
    decision = agent_router.decide_from_llm(
        message,
        llm_classifier=call_global_business_route_llm,
    )
    return BusinessIntent(decision.intent, decision.confidence, decision.source, decision.reason)

def append_mode_hint(answer: str, mode: str) -> str:
    answer = answer.strip()
    if mode == SESSION_MODE_ORDER:
        return f"{answer}\n\n你还在订单模式，要下单直接发订单文字、Excel 或照片；发“退出”可返回普通聊天。"
    if mode == SESSION_MODE_RECEIPT:
        return f"{answer}\n\n你还在入库模式，要记入库就发产成品照片；发“退出”可返回普通聊天。"
    return answer

def strip_order_inline_prefix(message: str) -> str | None:
    match = re.match(r"^\s*(订单|录单|下单)(?:\s*[：:]|\s+)(.+)$", message, flags=re.DOTALL)
    if not match:
        return None
    return match.group(2).strip()

def call_business_intent_llm(messages: list[dict[str, str]]) -> str:
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()

_DEFAULT_CHAT_SKILL = "你是微信公司客服。根据给你的【场景/事实】用自然、简短的人话回复客户，只能依据事实说话，严禁编造功能/数据/单号；涉及价格、交期、投诉、发票、退款等转人工，不要声称自己在入库/同步/保存。"
_DEFAULT_HELP_SKILL = "根据给你的【真实功能清单】用自然的话介绍给客户，严禁新增、暗示清单里没有的功能或命令。"


def load_chat_skill() -> str:
    """读取客服话术 skill（闲聊/引导/解释/错误引导共用）。每次读取，改了 .md 立即生效。"""
    try:
        text = CHAT_SKILL_FILE.read_text(encoding="utf-8").strip()
        return text or _DEFAULT_CHAT_SKILL
    except OSError as exc:
        logger.warning("chat_skill_load_failed file=%s error=%s", CHAT_SKILL_FILE, exc)
        return _DEFAULT_CHAT_SKILL


def load_help_skill() -> str:
    """读取功能介绍 skill（措辞由 LLM、功能清单由代码注入）。每次读取，改了 .md 立即生效。"""
    try:
        text = HELP_SKILL_FILE.read_text(encoding="utf-8").strip()
        return text or _DEFAULT_HELP_SKILL
    except OSError as exc:
        logger.warning("help_skill_load_failed file=%s error=%s", HELP_SKILL_FILE, exc)
        return _DEFAULT_HELP_SKILL


def llm_reply(skill: str, context: str, *, fallback: str = "") -> str:
    """统一"措辞"通道：skill(给 LLM 的指令) + context(代码给的真事实/用户消息) → LLM 组织成人话。

    走 call_business_intent_llm 通道。仅用于"不碰精确数据"或"精确数据由代码注入、LLM 只措辞"的回复；
    草稿回显 / 危险动作反馈 / 含精确数据(ID/数量/URL)的确认 一律不走这里（保持模板）。
    LLM 失败或空回复时返回 fallback（调用方给的安全确定性短句），保证不空。
    """
    try:
        reply = call_business_intent_llm(
            [
                {"role": "system", "content": skill},
                {"role": "user", "content": context},
            ]
        ).strip()
    except Exception as exc:
        logger.warning("llm_reply_failed error=%s", exc)
        return fallback
    return reply or fallback


# 危险动作（写库确认 / 撤销）AI 判的置信度门槛：不到一律当"否"，往不动数据兜底（安全边界 b）。
LLM_DANGER_THRESHOLD = 0.85


def _ai_danger_yes(message: str, system_prompt: str, key: str) -> bool:
    """危险动作 AI 判的统一通道：让 AI 输出 {key:bool, confidence:0~1}，
    只有 key 为真【且】confidence≥门槛才算 True。任何异常/超时/JSON 非法/置信不足 → False。
    这是"判用 AI、失败兜底不动数据"的落点：撤销/确认的 AI 判都走它。"""
    try:
        raw = call_business_intent_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ]
        )
        data = extract_json_object(raw)
    except Exception as exc:
        logger.warning("ai_danger_judge_failed key=%s error=%s", key, exc)
        return False
    if data.get(key) is not True:
        return False
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return confidence >= LLM_DANGER_THRESHOLD


def revoke_intent_is_real(message: str) -> bool:
    """撤销两道闸·第一道：AI 判用户是不是【明确要撤销】一笔已确认的订单/入库，
    而非疑问("能撤吗")、否定("别撤")、只是提到。AI 只提议（决定要不要发二次确认），不执行撤销。
    置信度<0.85 或失败 → False（不撤、不发二次确认）。"""
    return _ai_danger_yes(
        message,
        (
            "判断用户这句话是不是【明确要撤销/撤回】一笔已经确认入库的订单或入库记录。"
            "只输出 JSON：{\"revoke\":true,\"confidence\":0~1} 或 {\"revoke\":false,\"confidence\":0~1}。"
            "明确要撤=true；疑问(能撤吗/可以撤吗/怎么撤)、否定(别撤/先不撤/不用撤)、"
            "只是提到撤销=false。confidence 是你的把握度。不要输出别的。"
        ),
        "revoke",
    )


def revoke_confirm_is_real(message: str) -> bool:
    """撤销两道闸·第二道（替代旧的关键词"是"识别）：上一步已回显要撤的那单，
    AI 判用户这句是不是【明确确认执行撤销】(回 是/对/确认/撤吧 之类)。
    否定(不/别/算了)、疑问、改主意、别的话题、置信不足、失败 → False（不撤）。"""
    return _ai_danger_yes(
        message,
        (
            "上一步已经问用户'确认撤回吗'。判断用户这句是不是【明确确认、同意执行撤销】。"
            "只输出 JSON：{\"confirm\":true,\"confidence\":0~1} 或 {\"confirm\":false,\"confidence\":0~1}。"
            "是/对/确认/撤吧/嗯撤=true；否定(不/别/算了/先不)、疑问、改主意、别的话题=false。"
            "confidence 是你的把握度。不要输出别的。"
        ),
        "confirm",
    )


def peek_latest_cancellable_order(user_id: str) -> dict[str, Any] | None:
    """只读 peek：返回该用户最近一单"已确认未撤销"的订单 payload（含 raw_ref/status），用于二次确认回显。
    绝不改库。撤销三道闸的二次确认靠它逐字列出门店+商品+数量。"""
    candidates = [
        order
        for order in query_order_payloads()
        if str(order.get("raw_ref") or "") == user_id
        or str(order.get("raw_ref") or "").startswith(f"{user_id}:")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda order: int(order.get("id") or 0))


def classify_order_reply_intent(message: str, draft: dict[str, Any]) -> BusinessIntent:
    return classify_business_intent(
        message,
        has_draft=order_draft_has_content(draft),
        mode="order",
        draft_summary=format_order_draft_summary(draft),
        llm_classifier=call_business_intent_llm,
    )

def business_confirm_clarification(*, receipt: bool = False) -> str:
    subject = "这条入库记录" if receipt else "这张订单"
    return f"这句话我先不当成确认或修改，{subject}草稿保持不变。确认无误请回“确认 / 对 / ok / yes”；不要这单请回“取消”；要修改就直接发修改内容。"

def order_draft_reply(prefix: str, draft: dict[str, Any], missing: list[str]) -> str:
    summary = format_order_draft_summary(draft)
    if missing:
        return prefix + "\n" + summary + "\n" + missing_fields_reply(missing)
    return prefix + "\n" + summary + CONFIRM_HINT_CONTINUE_MODIFY

def receipt_draft_reply(prefix: str, draft: dict[str, Any], missing: list[str]) -> str:
    summary = format_receipt_draft_summary(draft)
    if missing:
        return prefix + "\n" + summary + "\n" + missing_fields_reply(missing, receipt=True)
    return prefix + "\n" + summary + CONFIRM_HINT_CONTINUE_MODIFY

def order_confirm_echo(draft: dict[str, Any]) -> str:
    # 确认两道闸·第一道回显：整单逐字走模板（format_order_draft_summary），AI 不碰数字（安全边界 a）。
    return (
        "请最后确认这单要写入订单库：\n"
        + format_order_draft_summary(draft)
        + "\n确认无误回“确认”写库；要改直接发修改内容；不要这单回“取消”。"
    )

def receipt_confirm_echo(draft: dict[str, Any]) -> str:
    # 同上，入库回显逐字走模板。
    return (
        "请最后确认这条要写入入库库：\n"
        + format_receipt_draft_summary(draft)
        + "\n确认无误回“确认”写库；要改直接发修改内容；不要这条回“取消”。"
    )

def save_confirmed_order_response(user_id: str, draft: dict[str, Any], history_length: int) -> ChatResponse:
    if not order_draft_has_content(draft):
        return ChatResponse(
            user_id=user_id,
            answer=NO_ORDER_DRAFT_REPLY,
            history_length=history_length,
        )
    missing = order_draft_missing_fields(draft)
    if missing:
        return ChatResponse(
            user_id=user_id,
            answer=missing_fields_reply(missing),
            history_length=history_length,
        )

    try:
        order_id, line_count = save_confirmed_order(user_id, draft)
    except Exception as exc:
        logger.warning("order_save_failed user_id=%s error=%s", user_id, exc)
        return ChatResponse(
            user_id=user_id,
            answer="订单保存失败了，请稍后再试，或联系管理员查看后台日志。",
            history_length=history_length,
        )

    clear_order_draft(user_id)
    return ChatResponse(
        user_id=user_id,
        answer=f"已保存订单入库，ID {order_id}，共 {line_count} 行商品。继续发下一张即可。",
        history_length=history_length + line_count,
    )

def load_order_skill() -> str:
    """读取订单 skill（规则 + 例子）。每次读取，方便你改了 .md 立即生效，不用重启想。"""
    try:
        return ORDER_SKILL_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("order_skill_load_failed file=%s error=%s", ORDER_SKILL_FILE, exc)
        return ""

def llm_order_draft_from_message(existing_draft: dict[str, Any], message: str) -> dict[str, Any] | None:
    """订单整理大脑：读 skill + 当前草稿 + 用户消息 → 输出【完整】更新后草稿。

    新单或加 / 改 / 删 / 换都统一走这里：LLM 负责理解并产出完整草稿，
    代码只做归一化与身份字段保护。失败返回 None，由调用方安全兜底。
    """
    skill = load_order_skill()
    today = datetime.now().date().isoformat()
    current = json.dumps(existing_draft or {}, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": skill},
                {
                    "role": "user",
                    "content": (
                        f"今天日期：{today}\n\n"
                        f"当前订单草稿（JSON，空对象 {{}} 表示还没有草稿）：\n{current}\n\n"
                        f"用户最新消息：\n{message}\n\n"
                        "请输出更新后的【完整】订单草稿 JSON：应用用户的加 / 改 / 删 / 换，"
                        "保留所有未改动的商品，只输出一个 JSON 对象。"
                    ),
                },
            ],
            temperature=0,
        )
    except Exception as exc:
        logger.warning("order_skill_llm_failed error=%s", exc)
        return None

    parsed = extract_json_object(response.choices[0].message.content or "")
    if not parsed:
        return None

    # 身份 / 系统字段以现有草稿为准，不让文字更新悄悄改掉。
    if existing_draft:
        for key in ("kind", "source", "order_no", "orderer", "raw_ref", "created_at"):
            if existing_draft.get(key):
                parsed[key] = existing_draft.get(key)

    if not existing_draft:
        # 文字加单独立成 base 订单，不再依附 excel 做 patch（修改既有草稿则沿用其身份字段，见上）
        parsed["kind"] = ORDER_KIND_BASE
        parsed["source"] = ORDER_SOURCE_TEXT

    # order_date 由四点线在 save_order_draft 统一盖（覆盖文字里写的日期），这里不再按显式日期定。
    parsed["confirmed"] = False
    parsed["status"] = ORDER_STATUS_NEW
    parsed["created_at"] = parsed.get("created_at") or now_iso()

    normalized = normalize_order_draft(parsed)
    if not normalized or not normalized.get("items"):
        return None
    return normalized

def trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    return history[-MAX_HISTORY_MESSAGES:]

def build_llm_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if PROMPT_TURN_CONTEXT:
        user_turns = sum(1 for message in history if message.get("role") == "user")
        messages.append(
            {
                "role": "system",
                "content": (
                    f"运行时信息：当前会话已收到 {user_turns} 个用户回合。"
                    "如果系统提示包含阶段流程，请用这个数字判断访谈节奏。"
                    "如果回合数只是建议，不要把它当作硬性拒答或卡死条件。"
                ),
            }
        )

    return [*messages, *history]

def call_llm(user_id: str, history: list[dict[str, str]]) -> str:
    messages = build_llm_messages(history)

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        print("========== LLM ERROR ==========")
        print(str(exc))
        traceback.print_exc()
        print("================================")
        logger.warning(
            "llm_failed user_id=%s history_length=%s model=%s error_type=%s",
            user_id,
            len(history),
            MODEL_NAME,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "LLM request failed",
                "real_error": str(exc),
            },
        ) from exc

    logger.info(
        "llm_success user_id=%s history_length=%s model=%s",
        user_id,
        len(history),
        MODEL_NAME,
    )
    return answer or ""

def call_customer_chat_llm(user_id: str, history: list[dict[str, str]]) -> str:
    messages = [{"role": "system", "content": CUSTOMER_CHAT_PROMPT}, *history]
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
        )
    except Exception as exc:
        logger.exception(
            "customer_chat_llm_error user_id=%s model=%s error_type=%s",
            user_id,
            MODEL_NAME,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "LLM request failed",
                "real_error": str(exc),
            },
        ) from exc

    return (response.choices[0].message.content or "").strip()

def handle_order_user_message(user_id: str, message: str, raw_ref: str | None = None) -> ChatResponse:
    command = normalize_command(message)
    history_length = user_order_count(user_id)
    existing_draft = get_order_draft(user_id)
    has_existing_draft = order_draft_has_content(existing_draft)

    if command in ORDER_CANCEL_COMMANDS:
        clear_order_draft(user_id, next_mode=SESSION_MODE_CHAT)
        return ChatResponse(
            user_id=user_id,
            answer=ORDER_DRAFT_CLEARED_REPLY,
            history_length=history_length,
        )

    if has_existing_draft:
        if is_order_draft_view_command(command):
            summary = format_order_draft_summary(existing_draft)
            return ChatResponse(
                user_id=user_id,
                answer=(
                    "当前订单草稿：\n"
                    + summary
                    + CONFIRM_HINT_MODIFY
                ),
                history_length=history_length,
            )

        intent = classify_order_reply_intent(message, existing_draft)
        if intent.intent != INTENT_CONFIRM:
            clear_pending_confirm(user_id)  # 任何非确认回复都打断"待确认"态，绝不延续到下一条
        if intent.intent == INTENT_CANCEL and intent.is_rule:
            clear_order_draft(user_id, next_mode=SESSION_MODE_CHAT)
            return ChatResponse(
                user_id=user_id,
                answer=ORDER_DRAFT_CLEARED_REPLY,
                history_length=history_length,
            )
        if intent.intent == INTENT_EXIT and intent.is_rule:
            return ChatResponse(
                user_id=user_id,
                answer=exit_business_mode(user_id),
                history_length=0,
            )
        if intent.intent == INTENT_CONFIRM:
            # 确认两道闸：一道 AI 判 confirm(≥0.85) → 不写、模板回显、set pending；
            # 二道 AI 再判 confirm → 才真写库。两次都是独立 AI 判，中间回显走模板（安全边界 a/b）。
            if get_pending_confirm(user_id) == "order":
                clear_pending_confirm(user_id)
                return save_confirmed_order_response(user_id, existing_draft, history_length)
            missing = order_draft_missing_fields(existing_draft)
            if missing:
                return ChatResponse(
                    user_id=user_id,
                    answer=missing_fields_reply(missing),
                    history_length=history_length,
                )
            set_pending_confirm(user_id, "order")
            return ChatResponse(
                user_id=user_id,
                answer=order_confirm_echo(existing_draft),
                history_length=history_length,
            )
        if intent.intent == INTENT_REJECT:
            return ChatResponse(
                user_id=user_id,
                answer="这单我先不保存。要修改就直接发修改内容；发“取消”可以清空草稿。",
                history_length=history_length,
            )
        if intent.intent == INTENT_MODIFY:
            # 加 / 改 / 删 / 换统一交给订单 skill 大脑，输出完整更新后草稿（旧项不丢）。
            updated_draft = llm_order_draft_from_message(existing_draft, message)
            if not updated_draft:
                return ChatResponse(
                    user_id=user_id,
                    answer="这条修改我没解析成功，麻烦把要加 / 改 / 删的商品和数量说清楚再发一次。",
                    history_length=history_length,
                )
            updated_draft["raw_ref"] = updated_draft.get("raw_ref") or raw_ref or user_id
            if updated_draft.get("kind") == ORDER_KIND_PATCH:
                updated_draft["raw_text"] = message
            save_order_draft(user_id, updated_draft)
            missing = order_draft_missing_fields(updated_draft)
            answer = order_draft_reply("已按你的修改更新订单草稿：", updated_draft, missing)
            return ChatResponse(
                user_id=user_id,
                answer=answer,
                history_length=history_length,
            )
        if intent.intent == INTENT_CHAT and not looks_like_order_message(message):
            # 草稿态闲聊：闲聊一句 + 提醒草稿还在。全程只读，绝不清草稿/切模式。
            chat = handle_general_chat(user_id, message)
            return ChatResponse(
                user_id=user_id,
                answer=chat.answer + "\n（你那单还在，确认请回“确认”，要改直接发修改内容。）",
                history_length=chat.history_length,
            )
        if intent.intent == INTENT_UNCLEAR and not looks_like_order_message(message):
            return ChatResponse(
                user_id=user_id,
                answer=business_confirm_clarification(),
                history_length=history_length,
            )

    if is_confirm_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=NO_ORDER_DRAFT_REPLY,
            history_length=history_length,
        )

    draft = llm_order_draft_from_message(existing_draft, message)
    if not draft:
        return ChatResponse(
            user_id=user_id,
            answer="这条订单我没有解析成功。请把门店、商品、数量说清楚再发一次，例如：老三家 鸡腿 20件。",
            history_length=history_length,
        )

    draft["raw_ref"] = draft.get("raw_ref") or raw_ref or user_id
    if draft.get("kind") == ORDER_KIND_PATCH:
        draft["raw_text"] = draft.get("raw_text") or message
    save_order_draft(user_id, draft)

    missing = order_draft_missing_fields(draft)
    answer = order_draft_reply("我整理成待确认订单：", draft, missing)

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=history_length,
    )

def format_receipt_draft_summary(draft: dict[str, Any]) -> str:
    if not draft:
        return "暂无入库草稿"
    lines = [
        f"入库日期：{draft.get('date') or '未填写'}",
    ]
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    if not items:
        lines.append("成品：未填写")
    else:
        # PG 启用时才显示 SKU 校准标记(✅匹配标准/⚠待核对)；SQLite 回退保持原样
        show_markers = models.is_enabled()
        lines.append("成品：")
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "未填写成品").strip()
            spec = str(item.get("spec") or "").strip()
            qty_unit = f"{item.get('qty') if item.get('qty') is not None else '未填写数量'}{item.get('unit') or ''}"
            if show_markers:
                # code 非空 = 校准匹配上标准 SKU；藏掉 sku_ 哈希码，只给 ✅/⚠ 标记
                if str(item.get("code") or "").strip():
                    parts = [name, spec, qty_unit]
                    lines.append(f"{index}. ✅ {' / '.join(part for part in parts if part)}")
                else:
                    parts = [f"⚠ {name}（未匹配到标准SKU，请核对）", spec, qty_unit]
                    lines.append(f"{index}. {' / '.join(part for part in parts if part)}")
            else:
                parts = [
                    str(item.get("code") or "").strip(),
                    name,
                    spec,
                    qty_unit,
                ]
                lines.append(f"{index}. {' / '.join(part for part in parts if part)}")
    return "\n".join(lines)

def load_receipt_skill() -> str:
    """读取入库 skill（规则 + 例子）。每次读取，改了 .md 立即生效。"""
    try:
        return RECEIPT_SKILL_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("receipt_skill_load_failed file=%s error=%s", RECEIPT_SKILL_FILE, exc)
        return ""

def llm_receipt_draft_from_message(existing_draft: dict[str, Any], message: str) -> dict[str, Any] | None:
    """入库整理大脑：读 skill + 当前草稿 + 用户消息 → 输出【完整】更新后入库草稿。

    加 / 改 / 删 / 换统一走这里；代码只做归一化与身份字段保护。失败返回 None，调用方兜底。
    """
    skill = load_receipt_skill()
    today = datetime.now().date().isoformat()
    current = json.dumps(existing_draft or {}, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": skill},
                {
                    "role": "user",
                    "content": (
                        f"今天日期：{today}\n\n"
                        f"当前入库草稿（JSON）：\n{current}\n\n"
                        f"用户最新消息：\n{message}\n\n"
                        "请输出更新后的【完整】入库草稿 JSON：应用用户的加 / 改 / 删 / 换，"
                        "保留所有未改动的成品，只输出一个 JSON 对象。"
                    ),
                },
            ],
            temperature=0,
        )
    except Exception as exc:
        logger.warning("receipt_skill_llm_failed error=%s", exc)
        return None

    parsed = extract_json_object(response.choices[0].message.content or "")
    if not parsed:
        return None

    # 身份 / 系统字段以现有草稿为准，不让文字更新悄悄改掉。
    if existing_draft:
        for key in ("status", "created_at", "id", "raw_ref"):
            if existing_draft.get(key):
                parsed[key] = existing_draft.get(key)

    normalized = normalize_receipt_payload(parsed)
    if not normalized or not normalized.get("items"):
        return None
    return normalized

def classify_receipt_reply_intent(message: str, draft: dict[str, Any]) -> BusinessIntent:
    return classify_business_intent(
        message,
        has_draft=receipt_draft_has_content(draft),
        mode="receipt",
        draft_summary=format_receipt_draft_summary(draft),
        llm_classifier=call_business_intent_llm,
    )

def save_confirmed_receipt_response(user_id: str, draft: dict[str, Any]) -> ChatResponse:
    if not receipt_draft_has_content(draft):
        return ChatResponse(
            user_id=user_id,
            answer=NO_RECEIPT_DRAFT_REPLY,
            history_length=0,
        )
    missing = receipt_missing_fields(draft)
    if missing:
        return ChatResponse(
            user_id=user_id,
            answer=missing_fields_reply(missing, receipt=True),
            history_length=0,
        )

    try:
        receipt_id, line_count = save_confirmed_receipt(draft)
    except Exception as exc:
        logger.warning("receipt_save_failed user_id=%s error=%s", user_id, exc)
        return ChatResponse(
            user_id=user_id,
            answer="入库记录保存失败了，请稍后再试，或联系管理员查看后台日志。",
            history_length=0,
        )

    clear_receipt_draft(user_id)
    return ChatResponse(
        user_id=user_id,
        answer=f"已保存产成品入库记录 {receipt_id}，共 {line_count} 行成品。",
        history_length=0,
    )

def handle_receipt_user_message(user_id: str, message: str) -> ChatResponse:
    command = normalize_command(message)
    draft = get_receipt_draft(user_id)
    has_existing_draft = receipt_draft_has_content(draft)
    if command in ORDER_CANCEL_COMMANDS:
        clear_receipt_draft(user_id, next_mode=SESSION_MODE_CHAT)
        return ChatResponse(
            user_id=user_id,
            answer=RECEIPT_DRAFT_CLEARED_REPLY,
            history_length=0,
        )

    if has_existing_draft:
        intent = classify_receipt_reply_intent(message, draft)
        if intent.intent != INTENT_CONFIRM:
            clear_pending_confirm(user_id)  # 任何非确认回复都打断"待确认"态
        if intent.intent == INTENT_CANCEL and intent.is_rule:
            clear_receipt_draft(user_id, next_mode=SESSION_MODE_CHAT)
            return ChatResponse(
                user_id=user_id,
                answer=RECEIPT_DRAFT_CLEARED_REPLY,
                history_length=0,
            )
        if intent.intent == INTENT_EXIT and intent.is_rule:
            return ChatResponse(
                user_id=user_id,
                answer=exit_business_mode(user_id),
                history_length=0,
            )
        if intent.intent == INTENT_CONFIRM:
            # 入库确认两道闸：一道回显当前入库草稿(模板逐字)、set pending；二道 AI 再判 confirm 才写库。
            if get_pending_confirm(user_id) == "receipt":
                clear_pending_confirm(user_id)
                return save_confirmed_receipt_response(user_id, draft)
            missing = receipt_missing_fields(draft)
            if missing:
                return ChatResponse(
                    user_id=user_id,
                    answer=missing_fields_reply(missing, receipt=True),
                    history_length=0,
                )
            set_pending_confirm(user_id, "receipt")
            return ChatResponse(
                user_id=user_id,
                answer=receipt_confirm_echo(draft),
                history_length=0,
            )
        if intent.intent == INTENT_REJECT:
            return ChatResponse(
                user_id=user_id,
                answer="这条入库记录我先不保存。要修改请重新发送照片；发“取消”可以清空草稿。",
                history_length=0,
            )
        if intent.intent == INTENT_MODIFY:
            # 加 / 改 / 删 / 换统一交给入库 skill 大脑，输出完整更新后草稿（旧成品不丢）。
            updated_draft = llm_receipt_draft_from_message(draft, message)
            if not updated_draft:
                return ChatResponse(
                    user_id=user_id,
                    answer="这条修改我没解析成功，麻烦把要加 / 改 / 删的成品和数量说清楚再发一次。",
                    history_length=0,
                )
            save_receipt_draft(user_id, updated_draft)
            missing = receipt_missing_fields(updated_draft)
            answer = receipt_draft_reply("已按你的修改更新入库草稿：", updated_draft, missing)
            return ChatResponse(user_id=user_id, answer=answer, history_length=0)
        if intent.intent == INTENT_CHAT:
            # 草稿态闲聊：闲聊一句 + 提醒入库草稿还在。全程只读，绝不清草稿/切模式。
            chat = handle_general_chat(user_id, message)
            return ChatResponse(
                user_id=user_id,
                answer=chat.answer + "\n（那条入库记录还在，确认请回“确认”，要改直接发修改内容。）",
                history_length=chat.history_length,
            )
        if intent.intent == INTENT_UNCLEAR:
            return ChatResponse(
                user_id=user_id,
                answer=business_confirm_clarification(receipt=True),
                history_length=0,
            )

    if is_confirm_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=NO_RECEIPT_DRAFT_REPLY,
            history_length=0,
        )

    return ChatResponse(
        user_id=user_id,
        answer="当前是入库模式。请发送产成品入库照片；识别后我会发清单给你确认。发“订单”可切到订单模式。",
        history_length=0,
    )

CALIBRATE_MIN_SCORE = 0.7  # 粗筛门槛：低于此相似度的候选根本不进 LLM（与品名硬闸 ACCEPT_SCORE 同档）


def calibrate_receipt_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """照片识别后、存草稿前的 SKU 校准。只在 PG 启用时跑；任何异常都兜底为不校准。

    只换 name/spec(+标准 code)，qty/unit 恒为照片值；匹配不上的行保留原值、code 留空，仍在草稿里。
    """
    if not models.is_enabled():
        return draft  # SQLite 回退：完全是现在的行为，零影响
    items = draft.get("items") if isinstance(draft.get("items"), list) else []
    if not items:
        return draft

    def _find(name: str) -> list[dict[str, Any]]:
        try:
            # 门槛从严：相似度不够高的根本不进候选，砍掉噪音也省 token
            return models.find_product_candidates(name, top_n=5, min_score=CALIBRATE_MIN_SCORE)
        except Exception as exc:
            logger.warning("sku_calibrate_find_failed name=%s error=%s", name, exc)
            return []

    def _judge(prompt: str) -> Any:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你只输出可解析 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        return extract_json_object(response.choices[0].message.content or "")

    def _debug(message: str) -> None:
        logger.info("sku_calibrate %s", message)

    try:
        draft["items"] = calibrate_receipt_items(
            items, find_candidates=_find, judge=_judge, debug=_debug
        )
    except Exception as exc:
        logger.warning("sku_calibrate_failed error=%s", exc)  # 整体异常 → 不校准，保留原识别
    return draft


def handle_receipt_photo_input(user_id: str, image_bytes: bytes, mime_type: str | None, raw_ref: str) -> ChatResponse:
    try:
        draft = llm_parse_receipt_photo(vision_client, VISION_MODEL, image_bytes, mime_type, raw_ref)
    except Exception as exc:
        logger.warning("receipt_photo_parse_failed user_id=%s raw_ref=%s error=%s", user_id, raw_ref, exc)
        if "vision model is not configured" in str(exc):
            answer = "入库照片已收到，但当前视觉模型还没配置好。请稍后再试，或先人工记录。"
        else:
            answer = "这张入库照片我没有识别成功。请重新拍清楚成品名称和数量后再发。"
        return ChatResponse(user_id=user_id, answer=answer, history_length=0)

    draft = calibrate_receipt_draft(draft)
    save_receipt_draft(user_id, draft)
    missing = receipt_missing_fields(draft)
    answer = receipt_draft_reply("我把照片识别成待确认入库记录：", draft, missing)
    return ChatResponse(user_id=user_id, answer=answer, history_length=0)

def needs_human_transfer(message: str) -> bool:
    command = normalize_command(message)
    return command_contains_any(
        command,
        {"价格", "多少钱", "报价", "投诉", "赔", "纠纷", "发票", "退款", "催单", "交期", "合同"},
    )

def handle_general_chat(user_id: str, message: str, mode_hint: str | None = None) -> ChatResponse:
    if needs_human_transfer(message):
        answer = HUMAN_TRANSFER_REPLY
        if mode_hint:
            answer = append_mode_hint(answer, mode_hint)
        return ChatResponse(user_id=user_id, answer=answer, history_length=0)

    memory_key = f"customer_chat:{user_id}"
    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.setdefault(memory_key, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

        user_message_ts = time.time()
        history.append(
            {
                "role": "user",
                "content": message,
                "created_at": now_iso(),
                "ts": user_message_ts,
            }
        )
        history = trim_history(history)
        memory[memory_key] = history

        logger.info("customer_chat_request user_id=%s history_length=%s", user_id, len(history))

        answer = call_customer_chat_llm(user_id, history).strip()
        if mode_hint:
            answer = append_mode_hint(answer, mode_hint)

        history.append(
            {
                "role": "assistant",
                "content": answer,
                "created_at": now_iso(),
                "ts": time.time(),
            }
        )
        history = trim_history(history)
        memory[memory_key] = history
        save_memory(memory)

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=len(history),
    )

def handle_user_message(user_id: str, message: str, raw_ref: str | None = None) -> ChatResponse:
    user_id = user_id.strip()
    message = message.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    command = normalize_command(message)
    inline_order_message = strip_order_inline_prefix(message)
    current_mode = get_session_mode(user_id)
    record = get_session_record(user_id)
    has_order_draft = has_raw_order_draft(record)
    has_receipt_draft = has_raw_receipt_draft(record)
    has_any_draft = has_order_draft or has_receipt_draft

    # 撤销两道闸·第二道：上一步已问"确认撤回？"，这步用 AI 判这句是不是确认撤（revoke_confirm_is_real，
    # 含置信度门槛与失败兜底；不再用关键词"是"）→ 是才扣扳机真撤；判否/失败 → 撤销作罢。
    pending_revoke = get_pending_revoke(user_id)
    if pending_revoke:
        clear_pending_revoke(user_id)  # 一次性：无论撤不撤，先清掉待撤态
        if revoke_confirm_is_real(message):
            if pending_revoke == "receipt":
                answer = cancel_latest_receipt_for_user(user_id)
            else:
                answer = cancel_latest_order_for_user(user_id)
            return ChatResponse(user_id=user_id, answer=answer, history_length=user_order_count(user_id))
        # AI 判不是确认（含失败兜底） → 撤销作罢，这条消息继续按正常流程处理（不 return，落到下面）

    if command in ORDER_EXPORT_COMMANDS:
        return ChatResponse(
            user_id=user_id,
            answer=build_order_export_message(),
            history_length=user_order_count(user_id),
        )

    if is_status_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=build_status_message(user_id),
            history_length=user_order_count(user_id),
        )

    if is_mode_help_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=build_mode_help_message(user_id),
            history_length=user_order_count(user_id),
        )

    if is_exit_mode_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=exit_business_mode(user_id),
            history_length=0,
        )

    if command in ORDER_CANCEL_COMMANDS:
        if has_receipt_draft and not has_order_draft:
            clear_receipt_draft(user_id, next_mode=SESSION_MODE_CHAT)
            answer = "已清空当前入库草稿，并回到普通聊天。"
        elif has_order_draft and not has_receipt_draft:
            clear_order_draft(user_id, next_mode=SESSION_MODE_CHAT)
            answer = "已清空当前订单草稿，并回到普通聊天。"
        else:
            record = get_session_record(user_id)
            record.pop("order_draft", None)
            record.pop("receipt_draft", None)
            record.pop("pending_confirm", None)  # 取消即时生效，连带清确认闸
            record["mode"] = SESSION_MODE_CHAT
            save_session_record(user_id, record)
            answer = "已清空当前草稿，并回到普通聊天。"
        return ChatResponse(user_id=user_id, answer=answer, history_length=0)

    if is_revoke_command(command):
        # 撤销两道闸·第一道：AI 判语境(要撤 vs 疑问/否定/只是提到)，含置信度门槛与失败兜底。AI 只提议，不扣扳机。
        if revoke_intent_is_real(message):
            if current_mode == SESSION_MODE_RECEIPT or is_receipt_revoke_target(command):
                # 入库撤销同样走两道闸；二次确认不带具体数据（receipt 查询无 raw_ref，无法只读定位那一条）。
                set_pending_revoke(user_id, "receipt")
                return ChatResponse(
                    user_id=user_id,
                    answer="确认撤回最近那条入库记录吗？回“是”确认，回别的就不撤。",
                    history_length=user_order_count(user_id),
                )
            payload = peek_latest_cancellable_order(user_id)
            if payload is None:
                return ChatResponse(
                    user_id=user_id,
                    answer="没找到你最近确认的订单，暂时没有可撤回的。",
                    history_length=user_order_count(user_id),
                )
            if str(payload.get("status") or "") == ORDER_STATUS_FETCHED:
                return ChatResponse(
                    user_id=user_id,
                    answer="这单已被排产/发货使用，不能直接撤回，需要联系数据部处理。",
                    history_length=user_order_count(user_id),
                )
            # 撤销两道闸·二次确认回显：逐字列出要撤的那单（门店+商品+数量，模板，不经 AI）。下一条由 AI 判是否确认。
            set_pending_revoke(user_id, "order")
            return ChatResponse(
                user_id=user_id,
                answer=f"确认撤回（{summarize_order_for_reply(payload)}）那单吗？回“是”确认，回别的就不撤。",
                history_length=user_order_count(user_id),
            )
        # AI 判"不是"（别撤销/我能撤吗）→ 不触发撤销，继续正常处理（不 return，落到下面）

    if needs_human_transfer(message):
        return ChatResponse(user_id=user_id, answer=HUMAN_TRANSFER_REPLY, history_length=0)

    if has_order_draft and has_receipt_draft:
        return ChatResponse(
            user_id=user_id,
            answer="我发现当前同时有订单草稿和入库草稿，状态不安全。请发“取消”清空后重新录入，避免把数据记串。",
            history_length=0,
        )

    if is_order_mode_command(command) and has_receipt_draft:
        return ChatResponse(
            user_id=user_id,
            answer=business_mode_switch_blocked_reply(SESSION_MODE_RECEIPT, SESSION_MODE_ORDER),
            history_length=0,
        )
    if is_receipt_mode_command(command) and has_order_draft:
        return ChatResponse(
            user_id=user_id,
            answer=business_mode_switch_blocked_reply(SESSION_MODE_ORDER, SESSION_MODE_RECEIPT),
            history_length=user_order_count(user_id),
        )

    if has_order_draft:
        return handle_order_user_message(user_id, message, raw_ref=raw_ref)
    if has_receipt_draft:
        return handle_receipt_user_message(user_id, message)

    if is_confirm_command(command, has_draft=has_any_draft):
        if current_mode == SESSION_MODE_ORDER:
            return handle_order_user_message(user_id, message, raw_ref=raw_ref)
        if current_mode == SESSION_MODE_RECEIPT:
            return handle_receipt_user_message(user_id, message)
        return ChatResponse(
            user_id=user_id,
            answer="现在没有待确认的业务草稿。要录订单发“订单”，要记入库发“入库”。",
            history_length=0,
        )

    if is_order_mode_command(command):
        blocked = try_switch_business_mode(user_id, SESSION_MODE_ORDER)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=user_order_count(user_id))
        return ChatResponse(
            user_id=user_id,
            answer=ENTER_ORDER_MODE_REPLY,
            history_length=user_order_count(user_id),
        )

    if is_receipt_mode_command(command):
        blocked = try_switch_business_mode(user_id, SESSION_MODE_RECEIPT)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=0)
        return ChatResponse(
            user_id=user_id,
            answer=ENTER_RECEIPT_MODE_REPLY,
            history_length=0,
        )

    if is_order_storage_query_command(command):
        return ChatResponse(
            user_id=user_id,
            answer=build_order_storage_query_reply(user_id),
            history_length=user_order_count(user_id),
        )

    if inline_order_message is not None:
        blocked = try_switch_business_mode(user_id, SESSION_MODE_ORDER)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=user_order_count(user_id))
        return handle_order_user_message(user_id, inline_order_message, raw_ref=raw_ref)

    if current_mode == SESSION_MODE_ORDER:
        # 订单模式里也不靠关键词硬判：一律问分诊大脑（规则在 skills/routing/SKILL.md，你可改）。
        # 只有判为"给了具体商品内容"才录单；问句/想改但没给内容 → 自然聊天接住，不怼"解析失败"。
        order_mode_route = agent_router.decide_from_llm(
            message,
            mode=SESSION_MODE_ORDER,
            llm_classifier=call_global_business_route_llm,
        )
        if order_mode_route.intent == agent_router.ROUTE_ORDER_TEXT:
            return handle_order_user_message(user_id, message, raw_ref=raw_ref)
        return handle_general_chat(user_id, message, mode_hint=SESSION_MODE_ORDER)

    if current_mode == SESSION_MODE_RECEIPT:
        return handle_general_chat(user_id, message, mode_hint=SESSION_MODE_RECEIPT)

    route = classify_global_business_route(message)
    if route.intent == GLOBAL_ROUTE_ORDER_TEXT:
        blocked = try_switch_business_mode(user_id, SESSION_MODE_ORDER)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=user_order_count(user_id))
        return handle_order_user_message(user_id, message, raw_ref=raw_ref)
    if route.intent == GLOBAL_ROUTE_ENTER_ORDER:
        blocked = try_switch_business_mode(user_id, SESSION_MODE_ORDER)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=user_order_count(user_id))
        return ChatResponse(
            user_id=user_id,
            answer=ENTER_ORDER_MODE_REPLY,
            history_length=user_order_count(user_id),
        )
    if route.intent == GLOBAL_ROUTE_ENTER_RECEIPT:
        blocked = try_switch_business_mode(user_id, SESSION_MODE_RECEIPT)
        if blocked:
            return ChatResponse(user_id=user_id, answer=blocked, history_length=0)
        return ChatResponse(
            user_id=user_id,
            answer=ENTER_RECEIPT_MODE_REPLY,
            history_length=0,
        )
    if route.intent == GLOBAL_ROUTE_ORDER_QUERY:
        return ChatResponse(
            user_id=user_id,
            answer=build_order_storage_query_reply(user_id),
            history_length=user_order_count(user_id),
        )

    return handle_general_chat(user_id, message)

def save_excel_order_payloads(file_bytes: bytes, raw_ref: str) -> list[dict[str, Any]]:
    payloads = parse_excel_order_payloads(file_bytes, raw_ref)
    saved: list[dict[str, Any]] = []
    for payload in payloads:
        payload["confirmed"] = True
        payload["status"] = ORDER_STATUS_NEW
        saved.append(insert_order_payload(payload))
    return saved

def format_saved_order_ids(saved_orders: list[dict[str, Any]]) -> str:
    ids = [str(order.get("id")) for order in saved_orders if order.get("id")]
    return "、".join(ids)

def excel_order_failure_reply(exc: Exception) -> str:
    error_text = str(exc)
    if any(marker in error_text for marker in ("not Excel", "not a valid .xlsx", "File is not a zip file")):
        return "Excel订单解析失败了。我这边收到的文件内容不像有效的 .xlsx，请重新发送原始 Excel 文件。"
    if "legacy .xls" in error_text:
        return "Excel订单解析失败了。当前只支持 .xlsx/.xlsm，请先把表格另存为 .xlsx 后再发。"
    if any(marker in error_text for marker in ("no order item rows", "header row not found")):
        return "Excel订单解析失败了。我没识别到连续的商品名称和数量数据区，请确认表头后面有实际订货数据。"
    return "Excel订单解析失败了。请确认文件是标准订单表，且表头后面包含商品和数量数据。"

def handle_excel_order_input(user_id: str, file_bytes: bytes, raw_ref: str) -> ChatResponse:
    started_at = time.perf_counter()
    try:
        payloads = parse_excel_order_payloads(file_bytes, raw_ref)
    except Exception as exc:
        logger.warning(
            "excel_order_import_failed user_id=%s raw_ref=%s size=%s signature=%s error=%s",
            user_id,
            raw_ref,
            len(file_bytes),
            excel_file_signature(file_bytes),
            exc,
            exc_info=True,
        )
        return ChatResponse(
            user_id=user_id,
            answer=excel_order_failure_reply(exc),
            history_length=user_order_count(user_id),
        )

    parsed_at = time.perf_counter()
    parsed_line_count = sum(len(payload.get("items") or []) for payload in payloads)
    logger.info(
        "excel_order_parse_done user_id=%s raw_ref=%s size=%s payloads=%s lines=%s elapsed_ms=%s",
        user_id,
        raw_ref,
        len(file_bytes),
        len(payloads),
        parsed_line_count,
        int((parsed_at - started_at) * 1000),
    )

    if len(payloads) != 1:
        logger.warning(
            "excel_order_multiple_payloads user_id=%s raw_ref=%s payload_count=%s elapsed_ms=%s",
            user_id,
            raw_ref,
            len(payloads),
            int((time.perf_counter() - started_at) * 1000),
        )
        return ChatResponse(
            user_id=user_id,
            answer="这份 Excel 里我解析出了多张订单。为避免记串，请按单张订单拆开发，或联系管理员处理。",
            history_length=user_order_count(user_id),
        )

    draft = normalize_order_draft(payloads[0])
    draft["confirmed"] = False
    draft["status"] = ORDER_STATUS_NEW
    draft["raw_ref"] = draft.get("raw_ref") or raw_ref
    save_started_at = time.perf_counter()
    save_order_draft(user_id, draft)
    save_ms = int((time.perf_counter() - save_started_at) * 1000)
    line_count = len(draft.get("items") or [])
    logger.info(
        "excel_order_draft_ready user_id=%s raw_ref=%s size=%s lines=%s parse_ms=%s draft_save_ms=%s elapsed_ms=%s",
        user_id,
        raw_ref,
        len(file_bytes),
        line_count,
        int((parsed_at - started_at) * 1000),
        save_ms,
        int((time.perf_counter() - started_at) * 1000),
    )
    summary = format_order_draft_summary(draft)
    return ChatResponse(
        user_id=user_id,
        answer=(
            f"我把 Excel 解析成待确认订单，共 {line_count} 行商品：\n"
            + summary
            + CONFIRM_HINT_MODIFY
        ),
        history_length=user_order_count(user_id),
    )

def handle_photo_order_input(user_id: str, image_bytes: bytes, mime_type: str | None, raw_ref: str) -> ChatResponse:
    try:
        draft = llm_parse_photo_order(vision_client, VISION_MODEL, image_bytes, mime_type, raw_ref)
    except Exception as exc:
        logger.warning("photo_order_parse_failed user_id=%s raw_ref=%s error=%s", user_id, raw_ref, exc)
        if "vision model is not configured" in str(exc):
            return ChatResponse(
                user_id=user_id,
                answer="照片订单识别已收到，但当前视觉模型还没配置好。请先用文字发送门店、商品和数量。",
                history_length=user_order_count(user_id),
            )
        return ChatResponse(
            user_id=user_id,
            answer="这张照片我没有识别成功。请重新拍清楚订单表，或直接用文字发送门店、商品和数量。",
            history_length=user_order_count(user_id),
        )

    draft["confirmed"] = False
    draft["status"] = ORDER_STATUS_NEW
    save_order_draft(user_id, draft)

    missing = order_draft_missing_fields(draft)
    summary = format_order_draft_summary(draft)
    if missing:
        answer = (
            "我先把照片识别成订单草稿，还缺："
            + "、".join(missing)
            + "\n"
            + summary
            + "\n请直接补充缺失信息，或发“取消”清空。"
        )
    else:
        answer = (
            "我把照片识别成待确认订单：\n"
            + summary
            + CONFIRM_HINT_MODIFY
        )

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=user_order_count(user_id),
    )


__all__ = [
    "call_global_business_route_llm",
    "classify_global_business_route",
    "append_mode_hint",
    "strip_order_inline_prefix",
    "call_business_intent_llm",
    "load_chat_skill",
    "load_help_skill",
    "llm_reply",
    "revoke_intent_is_real",
    "revoke_confirm_is_real",
    "peek_latest_cancellable_order",
    "classify_order_reply_intent",
    "business_confirm_clarification",
    "order_draft_reply",
    "receipt_draft_reply",
    "order_confirm_echo",
    "receipt_confirm_echo",
    "save_confirmed_order_response",
    "load_order_skill",
    "llm_order_draft_from_message",
    "trim_history",
    "build_llm_messages",
    "call_llm",
    "call_customer_chat_llm",
    "handle_order_user_message",
    "format_receipt_draft_summary",
    "load_receipt_skill",
    "llm_receipt_draft_from_message",
    "classify_receipt_reply_intent",
    "save_confirmed_receipt_response",
    "handle_receipt_user_message",
    "handle_receipt_photo_input",
    "needs_human_transfer",
    "handle_general_chat",
    "handle_user_message",
    "save_excel_order_payloads",
    "format_saved_order_ids",
    "excel_order_failure_reply",
    "handle_excel_order_input",
    "handle_photo_order_input",
]
