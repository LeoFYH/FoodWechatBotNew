import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RoutingStateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Windows 上 SQLite 连接未关时临时文件会被占用，导致清理报错；忽略清理异常即可（仅影响测试临时目录回收）。
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tempdir.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "dummy",
                "DATABASE_BACKEND": "sqlite",
                "MEMORY_FILE": str(root / "memory.json"),
                "SESSION_STATE_FILE": str(root / "session_state.json"),
                "ORDER_DB_FILE": str(root / "orders.db"),
                "RECEIPT_DB_FILE": str(root / "receipts.db"),
                "WECOM_KF_CURSOR_FILE": str(root / "kf_cursors.json"),
                "EXPORT_DIR": str(root / "exports"),
            },
            clear=False,
        )
        self.env_patch.start()
        os.environ.pop("VISION_API_KEY", None)
        sys.modules.pop("main", None)
        sys.modules.pop("dispatch", None)  # dispatch 引用 main 状态，重载须一起 pop 以重绑到新 main
        self.main = importlib.import_module("main")
        self.dispatch = importlib.import_module("dispatch")  # 分发逻辑已搬到 dispatch；patch 目标指向此处

    def tearDown(self) -> None:
        sys.modules.pop("main", None)
        sys.modules.pop("dispatch", None)
        self.env_patch.stop()
        self.tempdir.cleanup()

    def stub_chat(self):
        return patch.object(self.dispatch, "call_customer_chat_llm", return_value="普通回复")

    def test_llm_reply_passes_skill_and_context_via_intent_channel(self) -> None:
        # 阶段6 基建：llm_reply 把 skill 当 system、context 当 user，走 call_business_intent_llm 通道。
        captured = {}

        def fake(messages):
            captured["messages"] = messages
            return "  自然措辞回复  "

        with patch.object(self.dispatch, "call_business_intent_llm", side_effect=fake):
            out = self.dispatch.llm_reply("这是技能指令", "这是代码给的真实事实")

        self.assertEqual(out, "自然措辞回复")
        self.assertEqual(captured["messages"][0], {"role": "system", "content": "这是技能指令"})
        self.assertEqual(captured["messages"][1], {"role": "user", "content": "这是代码给的真实事实"})

    def test_llm_reply_returns_fallback_on_failure(self) -> None:
        # LLM 失败时返回调用方给的安全确定性短句，绝不空、绝不抛。
        with patch.object(self.dispatch, "call_business_intent_llm", side_effect=RuntimeError("boom")):
            out = self.dispatch.llm_reply("s", "c", fallback="我在，稍等。")
        self.assertEqual(out, "我在，稍等。")

    def test_question_with_confirm_word_transfers_to_human(self) -> None:
        response = self.main.handle_user_message("u1", "发票可以开吗")
        self.assertIn("转人工", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_CHAT)

    def test_order_query_does_not_switch_to_order_mode(self) -> None:
        response = self.main.handle_user_message("u1", "查一下订单")
        self.assertIn("订单只有", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_CHAT)

    def test_negated_receipt_text_does_not_enter_receipt_mode(self) -> None:
        with self.stub_chat():
            response = self.main.handle_user_message("u1", "不要入库")
        self.assertEqual(response.answer, "普通回复")
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_CHAT)

    def test_plain_order_text_routes_to_order_parser(self) -> None:
        expected = self.main.ChatResponse(user_id="u1", answer="订单解析", history_length=0)
        # 不再有 looks_like 关键词硬判：订单文字一律进 agent_router 分诊，这里 stub 路由大脑判为 order_text。
        with patch.object(
            self.dispatch,
            "call_global_business_route_llm",
            return_value='{"route":"order_text","confidence":0.9,"reason":"店+品+量"}',
        ), patch.object(self.dispatch, "handle_order_user_message", return_value=expected) as handler:
            response = self.main.handle_user_message("u1", "老三家 鸡腿 20件")
        handler.assert_called_once()
        self.assertEqual(response.answer, "订单解析")
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)

    def test_explicit_mode_commands_still_switch_modes(self) -> None:
        order_response = self.main.handle_user_message("u1", "订单")
        self.assertIn("进入订单模式", order_response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)

        receipt_response = self.main.handle_user_message("u2", "入库")
        self.assertIn("进入入库模式", receipt_response.answer)
        self.assertEqual(self.main.get_session_mode("u2"), self.main.SESSION_MODE_RECEIPT)

    def test_llm_global_route_can_enter_order_mode(self) -> None:
        with patch.object(
            self.dispatch,
            "call_global_business_route_llm",
            return_value='{"route":"enter_order","confidence":0.91,"reason":"要录订单"}',
        ):
            response = self.main.handle_user_message("u1", "门店要录一下")
        self.assertIn("进入订单模式", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)

    def test_order_draft_blocks_switch_to_receipt(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        response = self.main.handle_user_message("u1", "入库")
        self.assertIn("我先不切到入库模式", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))

    def test_save_order_draft_clears_receipt_draft(self) -> None:
        self.main.save_receipt_draft(
            "u1",
            {"date": "2026-06-24", "items": [{"name": "鲜肉馄饨", "qty": 10, "unit": "箱"}]},
        )
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        self.assertFalse(self.main.receipt_draft_has_content(self.main.get_receipt_draft("u1")))

    def test_cancel_clears_draft_and_exits_business_mode(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        response = self.main.handle_user_message("u1", "取消")
        self.assertIn("回到普通聊天", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_CHAT)
        self.assertFalse(self.main.order_draft_has_content(self.main.get_order_draft("u1")))

    def test_receipt_modify_routes_to_skill_and_saves(self) -> None:
        self.main.save_receipt_draft(
            "u1",
            {
                "date": "2026-06-24",
                "items": [
                    {"name": "鲜肉馄饨", "qty": 10, "unit": "箱"},
                    {"name": "虾肉馄饨", "qty": 6, "unit": "箱"},
                ],
            },
        )
        updated = self.main.normalize_receipt_payload(
            {
                "date": "2026-06-24",
                "items": [
                    {"name": "鲜肉馄饨", "qty": 20, "unit": "件"},
                    {"name": "虾肉馄饨", "qty": 6, "unit": "箱"},
                ],
            }
        )
        with patch.object(self.dispatch, "llm_receipt_draft_from_message", return_value=updated) as skill:
            response = self.main.handle_user_message("u1", "鲜肉馄饨改成20件")
        skill.assert_called_once()
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("已按你的修改更新入库草稿", response.answer)
        self.assertEqual(draft["items"][0]["qty"], 20)
        self.assertEqual([item["name"] for item in draft["items"]], ["鲜肉馄饨", "虾肉馄饨"])

    def test_order_modify_routes_to_skill_and_saves(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        updated = self.main.normalize_order_draft(
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "change_type": "add",
                "items": [
                    {"name": "鸡腿", "qty": 30, "unit": "件"},
                    {"name": "鸭腿", "qty": 8, "unit": "件"},
                ],
            }
        )
        with patch.object(self.dispatch, "llm_order_draft_from_message", return_value=updated) as skill:
            response = self.main.handle_user_message("u1", "鸡腿改成30件 再加鸭腿8件")
        skill.assert_called_once()
        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡腿", "鸭腿"])
        self.assertEqual(draft["items"][0]["qty"], 30)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)

    def test_order_modify_skill_failure_keeps_draft(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        with patch.object(self.dispatch, "llm_order_draft_from_message", return_value=None):
            response = self.main.handle_user_message("u1", "鸡腿改成30件")
        draft = self.main.get_order_draft("u1")
        self.assertIn("没解析成功", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡腿"])

    def test_ambiguous_done_reply_does_not_confirm_order_draft(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )

        # "没事了"被规则判为 CHAT → 走闲聊（stub）+ 草稿提醒；即便 LLM 想 confirm 也绝不保存。
        with patch.object(
            self.dispatch,
            "call_business_intent_llm",
            return_value='{"intent":"confirm","confidence":0.96,"reason":"用户说没事了"}',
        ), patch.object(self.dispatch, "call_customer_chat_llm", return_value="嗯，好的。"):
            response = self.main.handle_user_message("u1", "没事了")

        self.assertNotIn("已保存订单入库", response.answer)
        self.assertIn("确认", response.answer)  # 草稿提醒里有"确认请回'确认'"
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))
        self.assertEqual(self.main.user_order_count("u1"), 0)

    # ---- 草稿态"逃逸"：状态/help/拒绝/闲聊 都能被正确处理，且草稿绝不丢（铁律 b）----

    _ORDER_DRAFT_FIXTURE = {
        "kind": "patch",
        "source": "text",
        "store": "老三家",
        "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
        "change_type": "add",
    }
    _RECEIPT_DRAFT_FIXTURE = {"date": "2026-06-24", "items": [{"name": "鲜肉馄饨", "qty": 10, "unit": "箱"}]}

    def test_draft_state_status_query_keeps_draft(self) -> None:
        self.main.save_order_draft("u1", dict(self._ORDER_DRAFT_FIXTURE))
        response = self.main.handle_user_message("u1", "我在啥模式来着")
        self.assertIn("待确认", response.answer)  # build_status_message 报当前状态（有草稿待确认）
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))  # 草稿仍在

    def test_draft_state_help_query_keeps_draft(self) -> None:
        self.main.save_order_draft("u1", dict(self._ORDER_DRAFT_FIXTURE))
        response = self.main.handle_user_message("u1", "你有啥功能")
        self.assertIn("模式", response.answer)  # build_mode_help_message 介绍模式
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))  # 草稿仍在

    def test_draft_state_reject_keeps_receipt_draft(self) -> None:
        self.main.save_receipt_draft("u1", dict(self._RECEIPT_DRAFT_FIXTURE))
        response = self.main.handle_user_message("u1", "先不入库")
        self.assertIn("先不保存", response.answer)  # 拒绝走 REJECT 分支
        self.assertTrue(self.main.receipt_draft_has_content(self.main.get_receipt_draft("u1")))  # 草稿仍在

    def test_draft_state_chat_keeps_draft_and_reminds(self) -> None:
        self.main.save_order_draft("u1", dict(self._ORDER_DRAFT_FIXTURE))
        with patch.object(
            self.dispatch,
            "call_business_intent_llm",
            return_value='{"intent":"chat","confidence":0.95,"reason":"闲聊"}',
        ), patch.object(self.dispatch, "call_customer_chat_llm", return_value="今天挺好。"):
            response = self.main.handle_user_message("u1", "今天天气不错啊")
        self.assertIn("今天挺好", response.answer)  # 闲聊一句
        self.assertIn("那单还在", response.answer)  # 草稿提醒
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))  # 草稿仍在

    # ---- 危险动作语境修复：撤销三道闸 / "好吧"不算确认 / 提示语补取消出口 ----

    def test_reluctant_haoba_is_not_confirm_keeps_draft(self) -> None:
        # "好吧"勉强语气 → 不再被判成确认写库；走闲聊+提醒，草稿仍在。
        self.main.save_order_draft("u1", dict(self._ORDER_DRAFT_FIXTURE))
        with patch.object(self.dispatch, "call_customer_chat_llm", return_value="嗯好。"):
            response = self.main.handle_user_message("u1", "好吧")
        self.assertNotIn("已保存订单入库", response.answer)  # 没确认写库
        self.assertIn("那单还在", response.answer)  # 闲聊+草稿提醒
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))  # 草稿仍在

    def test_revoke_negation_or_question_does_not_revoke(self) -> None:
        # "别撤销""我可以撤销吗" → AI 判"不是" → 不触发撤销、不设 pending。
        with patch.object(self.dispatch, "revoke_intent_is_real", return_value=False), patch.object(
            self.dispatch, "call_customer_chat_llm", return_value="嗯好。"
        ):
            r1 = self.main.handle_user_message("u1", "别撤销")
            r2 = self.main.handle_user_message("u1", "我可以撤销吗")
        self.assertNotIn("确认撤回", r1.answer)
        self.assertNotIn("确认撤回", r2.answer)
        self.assertEqual(self.main.get_pending_revoke("u1"), "")  # 没进二次确认

    def test_revoke_three_gates_confirm_then_yes(self) -> None:
        # 三道闸：AI 判要撤 → 二次确认(带数据，未撤) → 用户"是" → 才真撤。
        self.main.insert_order_payload(
            {
                "kind": "base",
                "source": "excel",
                "store": "鼓楼店",
                "items": [{"name": "鲜肉馄饨", "qty": 20, "unit": "箱"}],
                "confirmed": True,
                "raw_ref": "u1",
            }
        )
        self.assertEqual(len(self.main.query_order_payloads()), 1)

        with patch.object(self.dispatch, "revoke_intent_is_real", return_value=True):
            confirm = self.main.handle_user_message("u1", "撤销上一单")
        self.assertIn("确认撤回", confirm.answer)  # 二次确认
        self.assertIn("鼓楼店", confirm.answer)  # 逐字带数据
        self.assertEqual(self.main.get_pending_revoke("u1"), "order")  # pending 已设
        self.assertEqual(len(self.main.query_order_payloads()), 1)  # 此刻还没撤

        response = self.main.handle_user_message("u1", "是")  # 第三道：代码识别"是"才撤
        self.assertIn("撤回了", response.answer)
        self.assertEqual(self.main.get_pending_revoke("u1"), "")  # pending 清
        self.assertEqual(len(self.main.query_order_payloads()), 0)  # 已撤

    def test_revoke_pending_non_yes_does_not_revoke(self) -> None:
        # pending 已设但用户回的不是"是" → 撤销作罢、清 pending、订单仍在。
        self.main.insert_order_payload(
            {
                "kind": "base",
                "source": "excel",
                "store": "鼓楼店",
                "items": [{"name": "鲜肉馄饨", "qty": 20, "unit": "箱"}],
                "confirmed": True,
                "raw_ref": "u1",
            }
        )
        self.main.set_pending_revoke("u1", "order")
        with patch.object(self.dispatch, "call_customer_chat_llm", return_value="嗯好。"):
            response = self.main.handle_user_message("u1", "算了不撤了")
        self.assertNotIn("撤回了", response.answer)
        self.assertEqual(self.main.get_pending_revoke("u1"), "")  # pending 清
        self.assertEqual(len(self.main.query_order_payloads()), 1)  # 订单仍在

    def test_draft_confirm_hint_mentions_cancel_exit(self) -> None:
        self.assertIn("取消", self.dispatch.CONFIRM_HINT_MODIFY)
        self.assertIn("取消", self.dispatch.CONFIRM_HINT_CONTINUE_MODIFY)

    def test_order_draft_view_command_shows_current_draft_without_llm(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [
                    {"name": "鸡腿", "qty": 20, "unit": "件"},
                    {"name": "鸭腿", "qty": 5, "unit": "件"},
                ],
                "change_type": "add",
            },
        )
        with patch.object(self.dispatch, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "我先看看这单有啥")

        self.assertIn("当前订单草稿", response.answer)
        self.assertIn("老三家", response.answer)
        self.assertIn("鸡腿", response.answer)
        self.assertIn("鸭腿", response.answer)
        self.assertNotIn("我不确定你是不是要保存", response.answer)
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))

    def test_order_draft_current_order_command_shows_current_draft_without_llm(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        with patch.object(self.dispatch, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "查看当前订单")

        self.assertIn("当前订单草稿", response.answer)
        self.assertIn("鸡腿", response.answer)
        self.assertNotIn("我不确定你是不是要保存", response.answer)

    def test_order_draft_repeat_command_shows_current_draft_without_llm(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "patch",
                "source": "text",
                "store": "老三家",
                "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
                "change_type": "add",
            },
        )
        with patch.object(self.dispatch, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "重复一遍订单")

        self.assertIn("当前订单草稿", response.answer)
        self.assertIn("鸡腿", response.answer)
        self.assertNotIn("我不确定你是不是要保存", response.answer)
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))

    def test_receipt_modify_skill_failure_keeps_draft(self) -> None:
        self.main.save_receipt_draft(
            "u1",
            {"date": "2026-06-24", "items": [{"name": "鲜肉馄饨", "qty": 10, "unit": "箱"}]},
        )
        with patch.object(self.dispatch, "llm_receipt_draft_from_message", return_value=None):
            response = self.main.handle_user_message("u1", "鲜肉馄饨改成20件")
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("没解析成功", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鲜肉馄饨"])

    def test_confirm_question_is_not_confirm_command(self) -> None:
        self.assertFalse(self.main.is_confirm_command("发票可以开吗", has_draft=True))
        self.assertFalse(self.main.is_confirm_command("这个可以吗", has_draft=True))


if __name__ == "__main__":
    unittest.main()
