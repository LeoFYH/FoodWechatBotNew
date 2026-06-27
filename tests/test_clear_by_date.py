import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


TOKEN = "test-robot-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
D1 = "2026-06-27"
D2 = "2026-06-28"


class ClearByDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tempdir.name)
        self.env_patch = patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "dummy",
                "ROBOT_API_TOKEN": TOKEN,
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
        # routers.robot 绑定 main 的函数与 DB 路径，必须随 main 一起重载，否则复用旧 main 的库路径
        for mod in ("routers.robot", "main", "dispatch"):
            sys.modules.pop(mod, None)
        self.main = importlib.import_module("main")
        self.client = TestClient(self.main.app)

    def tearDown(self) -> None:
        for mod in ("routers.robot", "main", "dispatch"):
            sys.modules.pop(mod, None)
        self.env_patch.stop()
        self.tempdir.cleanup()

    def _make_order(self, order_date, *, confirmed, status, kind="base"):
        self.main.insert_order_payload(
            {
                "kind": kind,
                "source": "excel",
                "store": "馄饨侯",
                "order_date": order_date,
                "status": status,
                "confirmed": confirmed,
                "items": [{"name": "鲜肉大馄饨", "qty": 3, "unit": "箱"}],
            }
        )

    def _make_receipt(self, date):
        self.main.insert_receipt_payload(
            {"date": date, "items": [{"name": "鲜肉大馄饨", "qty": 50, "unit": "箱"}]}
        )

    # ---------------- orders ----------------

    def test_orders_clear_deletes_all_kinds_keeps_other_day(self):
        # D1：混合 confirmed/new/fetched/base/patch；D2：保留对照
        self._make_order(D1, confirmed=True, status="new")
        self._make_order(D1, confirmed=False, status="new")
        self._make_order(D1, confirmed=True, status="fetched")
        self._make_order(D1, confirmed=True, status="new", kind="patch")
        self._make_order(D2, confirmed=True, status="new")

        resp = self.client.post("/api/orders/clear_by_date", json={"order_date": D1}, headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["order_date"], D1)
        self.assertEqual(body["deleted"], 4)
        self.assertEqual(len(body["deleted_ids"]), 4)

        # 删完 D1 的 new 和 all 都必须空
        for st in ("new", "all"):
            got = self.client.get(f"/api/orders?status={st}&order_date={D1}", headers=AUTH)
            self.assertEqual(got.json(), {"orders": []}, st)
        # D2 仍在
        self.assertEqual(len(self.client.get(f"/api/orders?status=all&order_date={D2}", headers=AUTH).json()["orders"]), 1)

    def test_orders_clear_empty_day_returns_zero(self):
        resp = self.client.post("/api/orders/clear_by_date", json={"order_date": D1}, headers=AUTH)
        self.assertEqual(resp.json(), {"ok": True, "order_date": D1, "deleted": 0, "deleted_ids": []})

    # ---------------- receipts ----------------

    def test_receipts_clear_deletes_day_keeps_other(self):
        self._make_receipt(D1)
        self._make_receipt(D1)
        self._make_receipt(D2)

        resp = self.client.post("/api/receipts/clear_by_date", json={"date": D1}, headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["date"], D1)
        self.assertEqual(body["deleted"], 2)
        self.assertTrue(all(rid.startswith("r") for rid in body["deleted_ids"]))  # r001 形式

        self.assertEqual(self.client.get(f"/api/receipts?date={D1}", headers=AUTH).json(), {"receipts": []})
        self.assertEqual(len(self.client.get(f"/api/receipts?date={D2}", headers=AUTH).json()["receipts"]), 1)

    # ---------------- auth / validation ----------------

    def test_requires_token(self):
        self.assertEqual(self.client.post("/api/orders/clear_by_date", json={"order_date": D1}).status_code, 401)
        self.assertEqual(self.client.post("/api/receipts/clear_by_date", json={"date": D1}).status_code, 401)

    def test_bad_date_rejected(self):
        r1 = self.client.post("/api/orders/clear_by_date", json={"order_date": "not-a-date"}, headers=AUTH)
        self.assertEqual(r1.status_code, 400)
        r2 = self.client.post("/api/receipts/clear_by_date", json={"date": "27/06/2026"}, headers=AUTH)
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
