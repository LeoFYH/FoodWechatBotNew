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
                "INTERVIEW_ARCHIVE_FILE": str(root / "interviews.json"),
                "WECOM_KF_CURSOR_FILE": str(root / "kf_cursors.json"),
                "EXPORT_DIR": str(root / "exports"),
            },
            clear=False,
        )
        self.env_patch.start()
        os.environ.pop("VISION_API_KEY", None)
        sys.modules.pop("main", None)
        self.main = importlib.import_module("main")

    def tearDown(self) -> None:
        sys.modules.pop("main", None)
        self.env_patch.stop()
        self.tempdir.cleanup()

    def stub_chat(self):
        return patch.object(self.main, "call_customer_chat_llm", return_value="普通回复")

    def test_question_with_confirm_word_transfers_to_human(self) -> None:
        response = self.main.handle_user_message("u1", "发票可以开吗")
        self.assertIn("转人工", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_INTERVIEW)

    def test_order_query_does_not_switch_to_order_mode(self) -> None:
        response = self.main.handle_user_message("u1", "查一下订单")
        self.assertIn("订单只有", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_INTERVIEW)

    def test_negated_receipt_text_does_not_enter_receipt_mode(self) -> None:
        with self.stub_chat():
            response = self.main.handle_user_message("u1", "不要入库")
        self.assertEqual(response.answer, "普通回复")
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_INTERVIEW)

    def test_plain_order_text_routes_to_order_parser(self) -> None:
        expected = self.main.ChatResponse(user_id="u1", answer="订单解析", history_length=0)
        with patch.object(self.main, "handle_order_user_message", return_value=expected) as handler:
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
            self.main,
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
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_INTERVIEW)
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
        with patch.object(self.main, "llm_receipt_draft_from_message", return_value=updated) as skill:
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
        with patch.object(self.main, "llm_order_draft_from_message", return_value=updated) as skill:
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
        with patch.object(self.main, "llm_order_draft_from_message", return_value=None):
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

        with patch.object(
            self.main,
            "call_business_intent_llm",
            return_value='{"intent":"confirm","confidence":0.96,"reason":"用户说没事了"}',
        ):
            response = self.main.handle_user_message("u1", "没事了")

        self.assertNotIn("已保存订单入库", response.answer)
        self.assertIn("确认", response.answer)
        self.assertTrue(self.main.order_draft_has_content(self.main.get_order_draft("u1")))
        self.assertEqual(self.main.user_order_count("u1"), 0)

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
        with patch.object(self.main, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
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
        with patch.object(self.main, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
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
        with patch.object(self.main, "call_business_intent_llm", side_effect=AssertionError("LLM should not be called")):
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
        with patch.object(self.main, "llm_receipt_draft_from_message", return_value=None):
            response = self.main.handle_user_message("u1", "鲜肉馄饨改成20件")
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("没解析成功", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鲜肉馄饨"])

    def test_confirm_question_is_not_confirm_command(self) -> None:
        self.assertFalse(self.main.is_confirm_command("发票可以开吗", has_draft=True))
        self.assertFalse(self.main.is_confirm_command("这个可以吗", has_draft=True))


if __name__ == "__main__":
    unittest.main()
