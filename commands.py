"""commands.py —— 命令分类规则层(从 main.py 原样搬出,P7)。

把一条已归一化的客服消息判定成"确认/取消/退出/撤回/进订单/进入库/查询/看草稿…"等命令意图。
全部是确定性字符串匹配规则。

铁律(原则 d):**危险动作(确认/取消/退出/撤回)的硬判必须保持规则制,绝不交大模型。**
本模块**只 import 标准库 re**,不 import 任何 LLM client、不 import main——结构上不可能把判断委托给大模型。
词表与判定与原 main 逐字一致。
"""

from __future__ import annotations

import re


ORDER_MODE_COMMANDS = {"订单", "录单", "下单", "订单模式", "开始订单", "开始录单"}
RECEIPT_MODE_COMMANDS = {"入库", "入库模式", "产成品入库", "开始入库", "成品入库"}
EXIT_MODE_COMMANDS = {"退出", "结束", "不弄了", "算了", "返回", "退出订单", "退出入库", "结束订单", "结束入库"}
STATUS_COMMANDS = {"状态", "我在哪", "我在哪儿", "当前状态", "现在状态", "现在是什么模式"}
MODE_HELP_COMMANDS = {"模式", "有哪些模式", "有什么模式", "你有哪些模式", "你有什么模式", "怎么用", "你会什么", "功能", "帮助"}
REVOKE_COMMANDS = {
    "撤回",
    "撤销",
    "撤回上一单",
    "撤销上一单",
    "撤回刚刚的入库",
    "撤销刚刚的入库",
    "撤回入库",
    "撤销入库",
    "删了上一单",
    "删了",
    "刚那个不对",
    "刚才那个不对",
    "上一单不对",
}
ORDER_EXPORT_COMMANDS = {"导出订单", "订单导出", "下载订单", "订单表", "导出订单表"}
ORDER_CONFIRM_COMMANDS = {"确认", "确认订单", "保存", "保存订单", "提交", "提交订单"}
CONFIRM_LIKE_KEYWORDS = {"确认", "确认无误", "没问题", "可以", "对的", "是的", "保存", "提交", "录入", "写库", "入数据库", "直接入库", "记下"}
ORDER_STORAGE_QUERY_KEYWORDS = {"入库结果", "同步结果", "订单库", "数据库", "拉订单库", "同步订单", "查订单", "查一下订单", "看一下入库"}
ORDER_CANCEL_COMMANDS = {"取消", "取消订单", "取消草稿", "清空", "清空订单", "清空草稿", "不要了"}
ORDER_DRAFT_VIEW_COMMANDS = {
    "当前订单",
    "订单草稿",
    "查看当前订单",
    "看当前订单",
    "看看当前订单",
    "查看订单草稿",
    "看订单草稿",
    "查看草稿",
    "看草稿",
}
ORDER_DRAFT_VIEW_KEYWORDS = {
    "当前订单",
    "订单草稿",
    "订单内容",
    "当前草稿",
    "看看这单",
    "看这单",
    "查看这单",
    "这单有啥",
    "这单有什么",
    "这张订单",
    "重复一遍订单",
    "重复订单",
    "复述订单",
    "再说一遍订单",
    "再发一遍订单",
    "订单再发一遍",
}
ORDER_QUERY_KEYWORDS = {"查", "查询", "看", "结果", "同步", "拉取", "有没有", "了吗", "是否", "状态"}
BUSINESS_NEGATION_KEYWORDS = {"不要", "不用", "别", "先别", "不需要", "取消", "撤回", "退"}
QUESTION_LIKE_KEYWORDS = {"吗", "么", "?", "？", "能不能", "可不可以", "是否", "怎么", "如何", "什么", "多少", "几号", "价格", "发票"}
SOFT_CONFIRM_COMMANDS = {
    "ok",
    "okay",
    "yes",
    "y",
    "可以",
    "可以的",
    "行",
    "行的",
    "好",
    "好的",
    "对",
    "对的",
    "是",
    "是的",
    "没错",
    "没问题",
    "确认无误",
    "记下",
    "录入",
    "写库",
    "入数据库",
    "直接入库",
}


def normalize_command(message: str) -> str:
    return re.sub(r"\s+", "", message.strip()).lower()


def command_contains_any(command: str, keywords: set[str]) -> bool:
    return any(keyword and keyword in command for keyword in keywords)


def is_exit_mode_command(command: str) -> bool:
    return command in EXIT_MODE_COMMANDS or command_contains_any(
        command,
        {"不弄", "算了", "退出", "返回普通", "结束订单", "结束入库"},
    )


def is_revoke_command(command: str) -> bool:
    return command in REVOKE_COMMANDS or command_contains_any(
        command,
        {"撤回", "撤销", "删了", "删除上一", "刚那个不对", "刚才那个不对", "上一单不对"},
    )


def is_receipt_revoke_target(command: str) -> bool:
    return command_contains_any(command, {"入库", "入库记录", "成品", "产成品"})


def is_status_command(command: str) -> bool:
    return command in STATUS_COMMANDS


def is_business_query_or_negated(command: str) -> bool:
    return command_contains_any(command, ORDER_QUERY_KEYWORDS | BUSINESS_NEGATION_KEYWORDS)


def is_order_mode_command(command: str) -> bool:
    if command in ORDER_MODE_COMMANDS:
        return True
    if is_business_query_or_negated(command):
        return False
    if command_contains_any(command, {"我要下单", "帮我下单", "要下单", "我要录单", "帮我录单", "要录单", "我要录一单", "帮我录一单", "录一单"}):
        return True
    if "订单" in command and command_contains_any(command, {"发订单", "传订单", "录订单", "下订单", "订单表", "订单图片", "订单照片", "订单模式", "开始订单"}):
        return True
    return False


def is_receipt_mode_command(command: str) -> bool:
    if command in RECEIPT_MODE_COMMANDS:
        return True
    if is_business_query_or_negated(command) or command_contains_any(command, QUESTION_LIKE_KEYWORDS):
        return False
    if "入库" in command and command_contains_any(command, {"开始", "发", "传", "照片", "图片", "模式", "产成品", "成品", "录", "记"}):
        return True
    return False


def is_mode_help_command(command: str) -> bool:
    if command in MODE_HELP_COMMANDS:
        return True
    return "模式" in command and command_contains_any(command, {"哪些", "什么", "有啥", "怎么", "功能"})


def is_question_like_command(command: str) -> bool:
    return command_contains_any(command, QUESTION_LIKE_KEYWORDS)


def is_confirm_command(command: str, *, has_draft: bool = True) -> bool:
    if is_question_like_command(command):
        return False
    if command in ORDER_CONFIRM_COMMANDS:
        return True
    if command_contains_any(command, {"取消", "撤回", "不对", "不是", "别", "不要"}):
        return False
    if not has_draft:
        return False
    if command in SOFT_CONFIRM_COMMANDS:
        return True
    return False


def is_order_storage_query_command(command: str) -> bool:
    return command_contains_any(command, ORDER_STORAGE_QUERY_KEYWORDS)


def is_order_draft_view_command(command: str) -> bool:
    return command in ORDER_DRAFT_VIEW_COMMANDS or command_contains_any(command, ORDER_DRAFT_VIEW_KEYWORDS)


__all__ = [
    # 命令词表常量
    "ORDER_MODE_COMMANDS",
    "RECEIPT_MODE_COMMANDS",
    "EXIT_MODE_COMMANDS",
    "STATUS_COMMANDS",
    "MODE_HELP_COMMANDS",
    "REVOKE_COMMANDS",
    "ORDER_EXPORT_COMMANDS",
    "ORDER_CONFIRM_COMMANDS",
    "CONFIRM_LIKE_KEYWORDS",
    "ORDER_STORAGE_QUERY_KEYWORDS",
    "ORDER_CANCEL_COMMANDS",
    "ORDER_DRAFT_VIEW_COMMANDS",
    "ORDER_DRAFT_VIEW_KEYWORDS",
    "ORDER_QUERY_KEYWORDS",
    "BUSINESS_NEGATION_KEYWORDS",
    "QUESTION_LIKE_KEYWORDS",
    "SOFT_CONFIRM_COMMANDS",
    # 分类函数
    "normalize_command",
    "command_contains_any",
    "is_exit_mode_command",
    "is_revoke_command",
    "is_receipt_revoke_target",
    "is_status_command",
    "is_business_query_or_negated",
    "is_order_mode_command",
    "is_receipt_mode_command",
    "is_mode_help_command",
    "is_question_like_command",
    "is_confirm_command",
    "is_order_storage_query_command",
    "is_order_draft_view_command",
]
