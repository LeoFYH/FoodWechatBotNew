import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from order_normalize import (
    business_order_date,
    format_order_draft_summary,
    order_cutoff_hint,
)


class BusinessOrderDateTests(unittest.TestCase):
    """4 点线：北京时间 16:00 前=今天，16:00 及以后=今天+1。"""

    def test_before_cutoff_is_today(self):
        self.assertEqual(business_order_date(datetime(2026, 6, 29, 15, 59)), "2026-06-29")
        self.assertEqual(business_order_date(datetime(2026, 6, 29, 0, 0)), "2026-06-29")

    def test_at_and_after_cutoff_is_tomorrow(self):
        self.assertEqual(business_order_date(datetime(2026, 6, 29, 16, 0)), "2026-06-30")
        self.assertEqual(business_order_date(datetime(2026, 6, 29, 16, 1)), "2026-06-30")
        self.assertEqual(business_order_date(datetime(2026, 6, 29, 23, 59)), "2026-06-30")

    def test_month_rollover(self):
        self.assertEqual(business_order_date(datetime(2026, 6, 30, 16, 0)), "2026-07-01")


class OrderCutoffHintTests(unittest.TestCase):
    def test_hint_when_rolled_to_tomorrow(self):
        hint = order_cutoff_hint("2026-06-30", now=datetime(2026, 6, 29, 16, 0))
        self.assertIn("明天", hint)
        self.assertIn("2026-06-30", hint)
        self.assertIn("晚一天", hint)

    def test_no_hint_when_today(self):
        self.assertEqual(order_cutoff_hint("2026-06-29", now=datetime(2026, 6, 29, 10, 0)), "")

    def test_no_hint_empty_date(self):
        self.assertEqual(order_cutoff_hint("", now=datetime(2026, 6, 29, 16, 0)), "")


class SummaryHintTests(unittest.TestCase):
    """提示随回显走模板，不过 LLM。用远期/过去日期保证断言与运行时间无关。"""

    def _draft(self, order_date):
        return {
            "kind": "base",
            "source": "text",
            "store": "老三家",
            "order_date": order_date,
            "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}],
        }

    def test_future_date_shows_hint(self):
        summary = format_order_draft_summary(self._draft("2099-01-01"))
        self.assertIn("晚一天", summary)

    def test_past_date_no_hint(self):
        summary = format_order_draft_summary(self._draft("2000-01-01"))
        self.assertNotIn("晚一天", summary)


# ---------- 集成：文字加单成 base + save_order_draft 盖 4 点线日期 ----------

class _FakeLLM:
    def __init__(self, content):
        self._content = content

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _FakeLLM._Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_FakeLLM._Choice(content)]

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *args, **kwargs):
        return _FakeLLM._Resp(self._content)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
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
        for mod in ("routers.robot", "main", "dispatch"):
            sys.modules.pop(mod, None)
        self.main = importlib.import_module("main")
        self.dispatch = importlib.import_module("dispatch")

    def tearDown(self):
        for mod in ("routers.robot", "main", "dispatch"):
            sys.modules.pop(mod, None)
        self.env_patch.stop()
        self.tempdir.cleanup()

    def test_save_order_draft_stamps_business_date(self):
        # 即使草稿带了别的 order_date，存草稿时也被 4 点线覆盖
        self.main.save_order_draft(
            "u1",
            {"kind": "base", "source": "text", "store": "老三家",
             "order_date": "2020-01-01", "items": [{"name": "鸡腿", "qty": 20, "unit": "件"}]},
        )
        stored = self.main.get_order_draft("u1")
        self.assertEqual(stored["order_date"], self.main.business_order_date())

    def test_text_order_becomes_base(self):
        # 文字加单 → 独立 base 订单（不再是 patch）
        fake = _FakeLLM('{"store":"老三家","items":[{"name":"鸡腿","qty":20,"unit":"件"}]}')
        with patch.object(self.dispatch, "client", fake), \
             patch.object(self.dispatch, "load_order_skill", return_value=""):
            draft = self.dispatch.llm_order_draft_from_message({}, "老三家 鸡腿20件")
        self.assertIsNotNone(draft)
        self.assertEqual(draft["kind"], "base")
        self.assertEqual(draft["source"], "text")


if __name__ == "__main__":
    unittest.main()
