import importlib
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook


class ExcelParsingTests(unittest.TestCase):
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

    def workbook_bytes(self, workbook: Workbook) -> bytes:
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_parse_flexible_headers_and_quantity_unit(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["收货门店", "鼓楼店", "订单日期", "2026-06-24"])
        sheet.append(["商品名称/规格", "订货数量(箱)", "单价"])
        sheet.append(["鲜肉馄饨 260g", 2, 128.5])

        payloads = self.main.parse_excel_order_payloads(self.workbook_bytes(workbook), "test.xlsx")

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["store"], "鼓楼店")
        self.assertEqual(payload["order_date"], "2026-06-24")
        self.assertEqual(payload["items"][0]["name"], "鲜肉馄饨 260g")
        self.assertEqual(payload["items"][0]["qty"], 2)
        self.assertEqual(payload["items"][0]["unit"], "箱")
        self.assertEqual(payload["items"][0]["price"], 128.5)

    def test_parse_order_sheet_when_active_sheet_is_not_order(self) -> None:
        workbook = Workbook()
        summary_sheet = workbook.active
        summary_sheet.title = "说明"
        summary_sheet.append(["这个 sheet 不是订单"])
        order_sheet = workbook.create_sheet("订单")
        order_sheet.append(["门店名称", "订单日期"])
        order_sheet.append(["商品名称（规格）", "数量（袋）"])
        order_sheet.append(["虾仁馄饨", 6])

        payloads = self.main.parse_excel_order_payloads(self.workbook_bytes(workbook), "multi.xlsx")

        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["store"], "未确认门店")
        self.assertEqual(payloads[0]["items"][0]["name"], "虾仁馄饨")
        self.assertEqual(payloads[0]["items"][0]["qty"], 6)
        self.assertEqual(payloads[0]["items"][0]["unit"], "袋")


if __name__ == "__main__":
    unittest.main()
