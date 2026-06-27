import unittest

from sku_calibrate import (
    apply_calibration,
    build_calibration_prompt,
    calibrate_receipt_items,
    normalize_matches,
)


# 候选结构与 models.find_product_candidates 返回一致：有 code/name/spec/score，绝无 qty
CAND_HUNTUN = {"product_id": 1, "code": "sku_huntun", "name": "鲜肉大馄饨", "spec": "340g*24", "score": 1.0}
CAND_ZHENGJIAO = {"product_id": 2, "code": "sku_zhengjiao", "name": "韭菜鸡蛋蒸饺", "spec": "500g", "score": 0.8}


def photo_item(name, qty, unit, spec=None):
    # 模拟照片识别行：code 永远空，qty/unit 是这次入库的真实计量
    return {"code": None, "name": name, "spec": spec, "unit": unit, "qty": qty}


class MatchedReplacesNameSpecKeepsQtyUnit(unittest.TestCase):
    """①匹配上 → 换 name/spec、写标准 code；qty 和 unit 恒为照片值。"""

    def test_matched(self):
        items = [photo_item("鲜肉大馄炖", 50, "箱", spec="潦草规格")]
        candidates = {0: [CAND_HUNTUN, CAND_ZHENGJIAO]}
        out = apply_calibration(items, candidates, {0: "sku_huntun"})
        self.assertEqual(out[0]["name"], "鲜肉大馄饨")  # 字典标准品名
        self.assertEqual(out[0]["spec"], "340g*24")      # 字典标准规格
        self.assertEqual(out[0]["code"], "sku_huntun")   # 标准 code 作标记
        self.assertEqual(out[0]["qty"], 50)              # 照片数量，绝不动
        self.assertEqual(out[0]["unit"], "箱")           # 照片单位，绝不动

    def test_qty_unit_from_photo_even_though_candidate_has_none(self):
        # 候选里根本没有 qty/unit 字段，结构上不可能覆盖
        self.assertNotIn("qty", CAND_HUNTUN)
        self.assertNotIn("unit", CAND_HUNTUN)
        items = [photo_item("鲜肉大馄炖", 7, "袋")]
        out = apply_calibration(items, {0: [CAND_HUNTUN]}, {0: "sku_huntun"})
        self.assertEqual(out[0]["qty"], 7)
        self.assertEqual(out[0]["unit"], "袋")


class SecondGateRejectsFabricatedCode(unittest.TestCase):
    """②LLM 编造 code / 返回候选外 code → 判未匹配。"""

    def test_fabricated_code(self):
        items = [photo_item("鲜肉大馄炖", 50, "箱")]
        out = apply_calibration(items, {0: [CAND_HUNTUN]}, {0: "sku_does_not_exist"})
        self.assertIsNone(out[0]["code"])
        self.assertEqual(out[0]["name"], "鲜肉大馄炖")  # 原识别名保留

    def test_code_from_another_lines_candidate(self):
        # 行0的候选只有馄饨；LLM 给了蒸饺的 code（不在行0候选内）→ 未匹配
        items = [photo_item("鲜肉大馄炖", 50, "箱")]
        out = apply_calibration(items, {0: [CAND_HUNTUN]}, {0: "sku_zhengjiao"})
        self.assertIsNone(out[0]["code"])
        self.assertEqual(out[0]["name"], "鲜肉大馄炖")


class UnmatchedKeptNotDropped(unittest.TestCase):
    """③④候选空 / LLM 失败 / 未匹配 → 保留原值 + code 空，该行仍在草稿里。"""

    def test_empty_candidates_no_llm_call(self):
        called = {"judge": False}

        def judge(_prompt):
            called["judge"] = True
            return {}

        items = [photo_item("没有相似品", 30, "袋")]
        out = calibrate_receipt_items(items, find_candidates=lambda _n: [], judge=judge)
        self.assertFalse(called["judge"])         # 全无候选 → 免调 LLM
        self.assertEqual(len(out), 1)             # 行没被排除
        self.assertIsNone(out[0]["code"])
        self.assertEqual(out[0]["name"], "没有相似品")
        self.assertEqual(out[0]["qty"], 30)

    def test_llm_failure_falls_back_to_all_unmatched(self):
        def judge(_prompt):
            raise RuntimeError("LLM down")

        items = [photo_item("鲜肉大馄炖", 50, "箱")]
        out = calibrate_receipt_items(
            items, find_candidates=lambda _n: [CAND_HUNTUN], judge=judge
        )
        self.assertEqual(len(out), 1)             # 没被排除
        self.assertIsNone(out[0]["code"])         # 兜底未匹配
        self.assertEqual(out[0]["name"], "鲜肉大馄炖")
        self.assertEqual(out[0]["qty"], 50)

    def test_mixed_matched_and_unmatched_all_present(self):
        items = [
            photo_item("鲜肉大馄炖", 50, "箱"),   # 会匹配
            photo_item("车间自制小料", 3, "桶"),  # 无候选
        ]

        def find(name):
            return [CAND_HUNTUN] if "馄" in name else []

        def judge(_prompt):
            return {"matches": [{"index": 0, "code": "sku_huntun"}, {"index": 1, "code": None}]}

        out = calibrate_receipt_items(items, find_candidates=find, judge=judge)
        self.assertEqual(len(out), 2)             # 两行都在
        self.assertEqual(out[0]["code"], "sku_huntun")
        self.assertEqual(out[0]["name"], "鲜肉大馄饨")
        self.assertIsNone(out[1]["code"])          # ⚠行仍写库，只是未标准化
        self.assertEqual(out[1]["name"], "车间自制小料")
        self.assertEqual(out[1]["qty"], 3)


class NameGateTests(unittest.TestCase):
    """品名硬闸：LLM 选定且 code 在候选内，但标准名与识别名相似度 < 0.7 → 推翻判未匹配。"""

    def test_disaster_blocked(self):
        # 模拟"侧门混入"：脏候选(油焖鸡)靠某种途径进了鸭蛋面的候选池(score 被抬高)，
        # 且 LLM 选了它、code 也在池内(过了二次设闸)——品名硬闸必须拦下。
        dirty = {"product_id": 9, "code": "sku_youmenji", "name": "黄豆油焖鸡", "spec": "1kg", "score": 0.9}
        items = [photo_item("冷冻熟鸭蛋面", 20, "箱")]
        out = apply_calibration(items, {0: [dirty]}, {0: "sku_youmenji"})
        self.assertIsNone(out[0]["code"])              # 被品名硬闸推翻
        self.assertEqual(out[0]["name"], "冷冻熟鸭蛋面")  # 保留原识别
        self.assertEqual(out[0]["qty"], 20)

    def test_close_name_passes_gate(self):
        # 字典里真有对应(冷冻熟制鸡蛋面那种)——高相似度，应 ✅
        cand = {"product_id": 3, "code": "sku_jdm", "name": "冷冻熟鸭蛋面", "spec": "2kg", "score": 1.0}
        items = [photo_item("冷冻熟鸭蛋面", 20, "箱")]
        out = apply_calibration(items, {0: [cand]}, {0: "sku_jdm"})
        self.assertEqual(out[0]["code"], "sku_jdm")    # 真对应，✅
        self.assertEqual(out[0]["spec"], "2kg")

    def test_borderline_below_floor_blocked(self):
        # 部分词重叠但标准名差异较大 → 即使 LLM 选了也推翻
        cand = {"product_id": 4, "code": "sku_x", "name": "熟鸭蛋", "spec": "", "score": 0.9}
        items = [photo_item("冷冻熟鸭蛋面", 20, "箱")]  # vs 熟鸭蛋 ≈ 0.667 < 0.7
        out = apply_calibration(items, {0: [cand]}, {0: "sku_x"})
        self.assertIsNone(out[0]["code"])
        self.assertEqual(out[0]["name"], "冷冻熟鸭蛋面")


class DebugCallbackTests(unittest.TestCase):
    def test_debug_emits_per_line_with_reason(self):
        logs: list[str] = []
        dirty = {"product_id": 9, "code": "sku_youmenji", "name": "黄豆油焖鸡", "spec": "1kg", "score": 0.9}
        items = [photo_item("冷冻熟鸭蛋面", 20, "箱")]
        apply_calibration(items, {0: [dirty]}, {0: "sku_youmenji"}, debug=logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn("冷冻熟鸭蛋面", logs[0])    # 识别名
        self.assertIn("黄豆油焖鸡:0.9", logs[0])  # 候选名:分
        self.assertIn("品名硬闸推翻", logs[0])     # 原因


class NormalizeMatchesTests(unittest.TestCase):
    def test_parses_list(self):
        parsed = {"matches": [{"index": 0, "code": "sku_a"}, {"index": 1, "code": None}]}
        self.assertEqual(normalize_matches(parsed), {0: "sku_a", 1: None})

    def test_empty_code_string_is_none(self):
        self.assertEqual(normalize_matches({"matches": [{"index": 0, "code": ""}]}), {0: None})

    def test_garbage_returns_empty(self):
        self.assertEqual(normalize_matches(None), {})
        self.assertEqual(normalize_matches({"nope": 1}), {})
        self.assertEqual(normalize_matches("not a dict"), {})


class BuildPromptTests(unittest.TestCase):
    def test_contains_recognized_and_candidates(self):
        items = [photo_item("鲜肉大馄炖", 50, "箱")]
        prompt = build_calibration_prompt(items, {0: [CAND_HUNTUN]})
        self.assertIn("鲜肉大馄炖", prompt)       # 识别行
        self.assertIn("sku_huntun", prompt)       # 候选 code
        self.assertIn("340g*24", prompt)          # 候选规格
        self.assertIn("只能填候选里出现过的 code", prompt)  # 设闸指令

    def test_no_candidates_shows_none_marker(self):
        items = [photo_item("没有相似品", 30, "袋")]
        prompt = build_calibration_prompt(items, {0: []})
        self.assertIn("（无）", prompt)


if __name__ == "__main__":
    unittest.main()
