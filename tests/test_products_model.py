import unittest
from unittest.mock import patch

from models import products


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records the last SQL/params and returns canned rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return FakeCursor(self.rows)


class GenerateCodeTests(unittest.TestCase):
    def test_deterministic_for_same_name_spec(self):
        a = products.generate_product_code("鲜肉馄饨", "500g/袋")
        b = products.generate_product_code("鲜肉馄饨", "500g/袋")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sku_"))

    def test_normalizes_whitespace(self):
        a = products.generate_product_code(" 鲜肉馄饨 ", "500g/袋")
        b = products.generate_product_code("鲜肉馄饨", "500g/袋")
        self.assertEqual(a, b)

    def test_differs_on_spec(self):
        a = products.generate_product_code("鲜肉馄饨", "500g/袋")
        b = products.generate_product_code("鲜肉馄饨", "1kg/袋")
        self.assertNotEqual(a, b)


class UpsertProductTests(unittest.TestCase):
    def setUp(self):
        # jsonb() needs psycopg; identity is enough to capture params here.
        patcher = patch.object(products, "jsonb", lambda v: v)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_generates_code_when_absent(self):
        conn = FakeConn(rows=[{"id": 7}])
        pid = products.upsert_product(conn, 1, name="鲜肉馄饨", spec="500g/袋")
        self.assertEqual(pid, 7)
        _sql, params = conn.calls[-1]
        expected_code = products.generate_product_code("鲜肉馄饨", "500g/袋")
        # params order: tenant_id, code, name, spec, ...
        self.assertEqual(params[0], 1)
        self.assertEqual(params[1], expected_code)
        self.assertEqual(params[2], "鲜肉馄饨")

    def test_explicit_code_wins(self):
        conn = FakeConn(rows=[{"id": 3}])
        products.upsert_product(conn, 1, name="鲜肉馄饨", spec="500g/袋", code="WT001")
        _sql, params = conn.calls[-1]
        self.assertEqual(params[1], "WT001")

    def test_rejects_empty_name(self):
        conn = FakeConn(rows=[{"id": 1}])
        with self.assertRaises(ValueError):
            products.upsert_product(conn, 1, name="   ")


class ListActiveProductsTests(unittest.TestCase):
    def test_maps_rows(self):
        conn = FakeConn(
            rows=[
                {
                    "id": 1,
                    "code": "sku_abc",
                    "name": "鲜肉馄饨",
                    "spec": "500g",
                    "unit": "袋",
                    "category": "馄饨",
                    "status": "active",
                    "metadata": {"k": "v"},
                }
            ]
        )
        out = products.list_active_products(conn, 1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "鲜肉馄饨")
        self.assertEqual(out[0]["metadata"], {"k": "v"})


class FindCandidatesTests(unittest.TestCase):
    def _rows(self):
        # product 1 has an alias; product 2 has none
        return [
            {"id": 1, "code": "sku_1", "name": "鲜肉大馄饨", "alias": "鲜肉馄饨"},
            {"id": 1, "code": "sku_1", "name": "鲜肉大馄饨", "alias": "猪肉馄饨"},
            {"id": 2, "code": "sku_2", "name": "韭菜鸡蛋蒸饺", "alias": None},
        ]

    def test_ranks_and_matches_alias(self):
        conn = FakeConn(rows=self._rows())
        out = products.find_product_candidates(conn, 1, "鲜肉馄饨", top_n=5)
        self.assertEqual(out[0]["product_id"], 1)
        # best match came from the exact alias, not the name
        self.assertEqual(out[0]["matched_on"], "alias")
        self.assertEqual(out[0]["matched_text"], "鲜肉馄饨")
        self.assertEqual(out[0]["score"], 1.0)

    def test_top_n_limit(self):
        conn = FakeConn(rows=self._rows())
        out = products.find_product_candidates(conn, 1, "鲜肉馄饨", top_n=1)
        self.assertEqual(len(out), 1)

    def test_min_score_filters(self):
        conn = FakeConn(rows=self._rows())
        out = products.find_product_candidates(conn, 1, "鲜肉馄饨", min_score=0.99)
        self.assertEqual([c["product_id"] for c in out], [1])

    def test_empty_query_returns_empty(self):
        conn = FakeConn(rows=self._rows())
        self.assertEqual(products.find_product_candidates(conn, 1, "  "), [])


if __name__ == "__main__":
    unittest.main()
