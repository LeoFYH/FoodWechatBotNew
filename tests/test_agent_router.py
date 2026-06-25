"""agent_router 前门路由的单元测试。

纯模块测试：不导入 main、不碰数据库，所以不受 Windows 临时文件清理问题影响，
跑起来又快又干净，用来锁定"大脑"的判断与安全护栏行为。
"""

import unittest

import agent_router as ar


class AgentRouterTests(unittest.TestCase):
    def test_complex_order_without_keywords_routes_to_order_text(self) -> None:
        """复杂、无关键词的订单消息也能被分诊为 order_text（旧关键词闸门会丢成聊天）。"""

        def stub(_messages):
            return (
                '{"route":"order_text","confidence":0.9,"reason":"店+品+量",'
                '"fields":{"store":"鼓楼店","items":[{"name":"鲜肉馄饨","quantity":20}]}}'
            )

        decision = ar.decide_from_llm("明天还是老地方那两样，跟上次一样安排一下", llm_classifier=stub)
        self.assertEqual(decision.intent, ar.ROUTE_ORDER_TEXT)
        self.assertTrue(decision.is_actionable)
        self.assertEqual(decision.fields.get("store"), "鼓楼店")

    def test_low_confidence_is_squashed_to_unclear(self) -> None:
        """置信度不达标 → 压成 unclear，交回旧逻辑兜底。"""

        def stub(_messages):
            return '{"route":"order_text","confidence":0.5}'

        decision = ar.decide_from_llm("嗯嗯随便聊聊", llm_classifier=stub)
        self.assertEqual(decision.intent, ar.ROUTE_UNCLEAR)

    def test_llm_failure_falls_back_to_unclear(self) -> None:
        """大模型调用抛异常 → 不瞎猜，返回 unclear/llm_error。"""

        def stub(_messages):
            raise RuntimeError("network down")

        decision = ar.decide_from_llm("x", llm_classifier=stub)
        self.assertEqual(decision.intent, ar.ROUTE_UNCLEAR)
        self.assertEqual(decision.source, "llm_error")

    def test_markdown_wrapped_json_is_parsed(self) -> None:
        """模型多嘴包了 markdown 代码块也能稳健解析。"""

        decision = ar.parse_route_decision('```json\n{"route":"order_query","confidence":0.95}\n```')
        self.assertEqual(decision.intent, ar.ROUTE_ORDER_QUERY)
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_invalid_route_becomes_unclear(self) -> None:
        """模型给出非法 route → unclear，不会误触发业务动作。"""

        decision = ar.parse_route_decision('{"route":"banana","confidence":0.99}')
        self.assertEqual(decision.intent, ar.ROUTE_UNCLEAR)

    def test_confirm_like_message_is_not_actionable_route(self) -> None:
        """确认/取消/退出/撤回不归路由层判定，落到非 actionable，由确定性代码处理。"""

        def stub(_messages):
            return '{"route":"chat","confidence":0.9}'

        decision = ar.decide_from_llm("确认", llm_classifier=stub)
        self.assertFalse(decision.is_actionable)


if __name__ == "__main__":
    unittest.main()
