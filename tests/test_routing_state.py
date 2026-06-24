import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RoutingStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
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

    def test_receipt_modify_updates_single_item_quantity(self) -> None:
        self.main.save_receipt_draft(
            "u1",
            {"date": "2026-06-24", "items": [{"name": "鲜肉馄饨", "qty": 10, "unit": "箱"}]},
        )
        response = self.main.handle_user_message("u1", "数量改成20件")
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("已按你的修改更新入库草稿", response.answer)
        self.assertEqual(draft["items"][0]["qty"], 20)
        self.assertEqual(draft["items"][0]["unit"], "件")

    def test_order_cancel_single_item_keeps_remaining_draft(self) -> None:
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
        response = self.main.handle_user_message("u1", "取消鸡腿")
        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_ORDER)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸭腿"])

    def test_order_modify_named_item_quantity_without_llm(self) -> None:
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
        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "鸡腿数量改成30件")
        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual(draft["items"][0]["qty"], 30)
        self.assertEqual(draft["items"][0]["unit"], "件")
        self.assertEqual(draft["items"][1]["qty"], 5)

    def test_order_add_item_to_existing_draft_without_llm(self) -> None:
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
        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "再加鸭腿5件")
        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡腿", "鸭腿"])
        self.assertEqual(draft["items"][1]["qty"], 5)
        self.assertEqual(draft["items"][1]["unit"], "件")

    def test_order_add_item_missing_quantity_repeats_updated_draft(self) -> None:
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
        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "再加一个牛肉烧麦 数量我待会告诉你")

        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertIn("鸡腿", response.answer)
        self.assertIn("牛肉烧麦", response.answer)
        self.assertIn("第2项数量", response.answer)
        self.assertIn("补我一下", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡腿", "牛肉烧麦"])
        self.assertIsNone(draft["items"][1]["qty"])

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

    def test_order_add_shared_quantity_items_splits_each_item(self) -> None:
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

        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "再加猪肉烧卖 牛肉烧卖各10斤")

        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡腿", "猪肉烧卖", "牛肉烧卖"])
        self.assertEqual(draft["items"][1]["qty"], 10)
        self.assertEqual(draft["items"][1]["unit"], "斤")
        self.assertEqual(draft["items"][2]["qty"], 10)
        self.assertEqual(draft["items"][2]["unit"], "斤")

    def test_order_replace_item_then_add_shared_quantity_items(self) -> None:
        self.main.save_order_draft(
            "u1",
            {
                "kind": "base",
                "source": "photo",
                "store": "北京航食",
                "order_no": "北京航食-2026-06-16",
                "orderer": "王丽璞",
                "order_date": "2026-06-16",
                "deliver_date": "2026-06-17",
                "items": [
                    {
                        "code": "101205032",
                        "name": "冷冻熟制鸡蛋面",
                        "spec": "160克*64块",
                        "unit": "箱",
                        "qty": 1,
                        "price": 716.8,
                        "category": "冷冻熟食品库",
                    }
                ],
            },
        )

        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message("u1", "鸡蛋面取消 换成小麦面 然后加猪肉烧卖 牛肉烧卖各10斤")

        draft = self.main.get_order_draft("u1")
        self.assertIn("已按你的修改更新订单草稿", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["冷冻熟制小麦面", "猪肉烧卖", "牛肉烧卖"])
        self.assertEqual(draft["items"][0]["qty"], 1)
        self.assertEqual(draft["items"][1]["qty"], 10)
        self.assertEqual(draft["items"][1]["unit"], "斤")
        self.assertEqual(draft["items"][2]["qty"], 10)
        self.assertEqual(draft["items"][2]["unit"], "斤")

    def test_overly_complex_order_update_keeps_existing_draft_unchanged(self) -> None:
        original = {
            "kind": "patch",
            "source": "text",
            "store": "老三家",
            "items": [
                {"name": "鸡蛋面", "qty": 1, "unit": "箱"},
                {"name": "鸭腿", "qty": 8, "unit": "件"},
                {"name": "馄饨", "qty": 3, "unit": "箱"},
            ],
            "change_type": "add",
        }
        self.main.save_order_draft("u1", original)

        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message(
                "u1",
                "鸡蛋面取消换成小麦面然后加猪肉烧卖牛肉烧卖各10斤再把鸭腿改成5件顺便不要馄饨",
            )

        draft = self.main.get_order_draft("u1")
        self.assertIn("这句话包含的动作太多", response.answer)
        self.assertEqual([item["name"] for item in draft["items"]], ["鸡蛋面", "鸭腿", "馄饨"])
        self.assertEqual([item["qty"] for item in draft["items"]], [1, 8, 3])

    def test_overly_complex_order_text_does_not_enter_draft(self) -> None:
        with patch.object(self.main, "llm_parse_order_draft", side_effect=AssertionError("LLM should not be called")):
            response = self.main.handle_user_message(
                "u1",
                "老三家鸡蛋面取消换成小麦面然后加猪肉烧卖牛肉烧卖各10斤再把鸭腿改成5件顺便不要馄饨",
            )

        self.assertIn("这句话包含的动作太多", response.answer)
        self.assertFalse(self.main.order_draft_has_content(self.main.get_order_draft("u1")))
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_INTERVIEW)

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

    def test_receipt_modify_named_item_quantity_with_multiple_items(self) -> None:
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
        response = self.main.handle_user_message("u1", "鲜肉馄饨数量改成20件")
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("已按你的修改更新入库草稿", response.answer)
        self.assertEqual(draft["items"][0]["qty"], 20)
        self.assertEqual(draft["items"][0]["unit"], "件")
        self.assertEqual(draft["items"][1]["qty"], 6)

    def test_receipt_cancel_single_item_keeps_remaining_draft(self) -> None:
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
        response = self.main.handle_user_message("u1", "取消鲜肉馄饨")
        draft = self.main.get_receipt_draft("u1")
        self.assertIn("已按你的修改更新入库草稿", response.answer)
        self.assertEqual(self.main.get_session_mode("u1"), self.main.SESSION_MODE_RECEIPT)
        self.assertEqual([item["name"] for item in draft["items"]], ["虾肉馄饨"])

    def test_confirm_question_is_not_confirm_command(self) -> None:
        self.assertFalse(self.main.is_confirm_command("发票可以开吗", has_draft=True))
        self.assertFalse(self.main.is_confirm_command("这个可以吗", has_draft=True))


if __name__ == "__main__":
    unittest.main()
