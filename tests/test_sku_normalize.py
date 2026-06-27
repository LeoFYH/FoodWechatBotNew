import unittest

from models.products import generate_product_code
from sku_normalize import (
    normalize_category,
    normalize_name,
    normalize_sku,
    normalize_spec,
    normalize_unit,
)


def code_of(name, spec):
    n = normalize_sku(name=name, spec=spec)
    return generate_product_code(n["name"], n["spec"])


class SameSkuSameCodeTests(unittest.TestCase):
    """同一商品的不同写法，归一化后 code 必须相同。"""

    def test_fullwidth_halfwidth(self):
        self.assertEqual(code_of("鲜肉大馄饨", "340g*24"), code_of("鲜肉大馄饨", "３４０ｇ＊２４"))

    def test_cross_symbols(self):
        base = code_of("鲜肉大馄饨", "340g*24")
        for variant in ("340g×24", "340g✕24", "340g＊24", "340G*24"):
            self.assertEqual(code_of("鲜肉大馄饨", variant), base, variant)

    def test_letter_x_between_digits(self):
        base = code_of("某商品", "24*6")
        for variant in ("24x6", "24X6", "24×6"):
            self.assertEqual(code_of("某商品", variant), base, variant)

    def test_spaces(self):
        # spec 全删空格；name 合并空白为单空格
        self.assertEqual(code_of("鲜肉大馄饨", "340 g * 24"), code_of("鲜肉大馄饨", "340g*24"))
        self.assertEqual(code_of("鲜肉 大馄饨", "x"), code_of("鲜肉  大馄饨", "x"))

    def test_case_in_spec(self):
        self.assertEqual(code_of("白砂糖", "25KG"), code_of("白砂糖", "25kg"))


class DifferentSpecDifferentCodeTests(unittest.TestCase):
    """反向断言：规格数值不同（绝不被归一化合并）→ code 必须不同。"""

    def test_trailing_count_differs(self):
        self.assertNotEqual(code_of("速冻蒸饺", "1.28kg8"), code_of("速冻蒸饺", "1.28kg4"))

    def test_multiplier_differs(self):
        self.assertNotEqual(code_of("鲜肉大馄饨", "340g*24"), code_of("鲜肉大馄饨", "340g*12"))

    def test_weight_value_differs(self):
        self.assertNotEqual(code_of("白砂糖", "25kg"), code_of("白砂糖", "50kg"))

    def test_name_differs(self):
        self.assertNotEqual(code_of("鲜肉大馄饨", "340g*24"), code_of("猪肉大馄饨", "340g*24"))

    def test_name_space_not_removed(self):
        # name 只合并空白、不全删 → 有空格与无空格是不同商品，绝不合并
        self.assertNotEqual(code_of("鲜肉 大馄饨", "x"), code_of("鲜肉大馄饨", "x"))


class NormalizeUnitTests(unittest.TestCase):
    """单位别名：同单位不同叫法归一；跨量级绝不合并。"""

    def test_kg_group(self):
        for v in ("kg", "KG", "Kg", "千克", "公斤", "ｋｇ"):
            self.assertEqual(normalize_unit(v), "kg", v)

    def test_g_group(self):
        for v in ("g", "G", "克"):
            self.assertEqual(normalize_unit(v), "g", v)

    def test_ml_group(self):
        for v in ("ml", "ML", "mL", "毫升"):
            self.assertEqual(normalize_unit(v), "ml", v)

    def test_l_group(self):
        for v in ("l", "L", "升"):
            self.assertEqual(normalize_unit(v), "L", v)

    def test_cross_magnitude_never_merged(self):
        # g 与 kg、ml 与 L 必须互不相同
        self.assertNotEqual(normalize_unit("g"), normalize_unit("kg"))
        self.assertNotEqual(normalize_unit("ml"), normalize_unit("L"))

    def test_jin_not_kg(self):
        # 斤(=500g) 绝不并入 kg，保留原样
        self.assertEqual(normalize_unit("斤"), "斤")
        self.assertNotEqual(normalize_unit("斤"), "kg")

    def test_unknown_unit_kept(self):
        for v in ("箱", "袋", "个", "包", "桶"):
            self.assertEqual(normalize_unit(v), v, v)

    def test_empty(self):
        self.assertEqual(normalize_unit(""), "")
        self.assertEqual(normalize_unit(None), "")


class NormalizeFieldTests(unittest.TestCase):
    def test_name_collapses_and_strips(self):
        self.assertEqual(normalize_name("  鲜肉  大馄饨 "), "鲜肉 大馄饨")

    def test_name_empty(self):
        self.assertEqual(normalize_name("   "), "")
        self.assertEqual(normalize_name(None), "")

    def test_spec_removes_all_spaces(self):
        self.assertEqual(normalize_spec(" 340 g * 24 "), "340g*24")

    def test_spec_preserves_numbers(self):
        # 数值一个都不能动
        self.assertEqual(normalize_spec("1.28kg8"), "1.28kg8")

    def test_category(self):
        self.assertEqual(normalize_category(" 馄饨  类 "), "馄饨 类")


if __name__ == "__main__":
    unittest.main()
