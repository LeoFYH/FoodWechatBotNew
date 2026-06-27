"""sku_normalize.py —— 入库 SKU 主数据归一化纯函数层。

只统一"写法"，绝不改动任何"数值含义"：
- 写法（会统一）：全角/半角、乘号符号（× ✕ * 及数字间的 x/X）、空格、spec 内大小写、单位别名。
- 数值（绝不动）：规格里的数字、数量、量级一个都不改——`1.28kg8` 与 `1.28kg4` 永远是两个 SKU。

单位别名只在同一单位的不同叫法间归一（千克=公斤=kg），跨量级（g↔kg、ml↔L）绝不映射。

纯函数：仅依赖标准库 unicodedata + re，不 import main，可被独立测试与 `from sku_normalize import *` 门面 re-export。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


_CROSS_CANON = "*"

# 单位同义词表：每组都是「同一单位」的不同叫法 → 归一形态。
# 跨量级（g↔kg、ml↔L）绝不放进同一组；斤(=500g)不并入 kg。
_UNIT_ALIASES: dict[str, set[str]] = {
    "kg": {"kg", "千克", "公斤"},
    "g": {"g", "克"},
    "ml": {"ml", "毫升"},
    "L": {"l", "升"},  # 归一到大写 L，与数字 1 区分
}

# 反查表：小写别名 → 归一形态
_UNIT_CANON: dict[str, str] = {}
for _canon, _names in _UNIT_ALIASES.items():
    for _name in _names:
        _UNIT_CANON[_name.lower()] = _canon


def _to_halfwidth(text: str) -> str:
    """NFKC：全角数字/字母/标点/空格 → 半角，并展开 ㎏ 等兼容符号。不改数值。"""
    return unicodedata.normalize("NFKC", text)


def _unify_cross(text: str) -> str:
    """乘号写法统一为 *，并删掉乘号两侧空格。只动符号，不动两边数字。"""
    text = text.replace("×", _CROSS_CANON).replace("✕", _CROSS_CANON)
    # 字母 x/X 仅当「两侧贴数字」才算乘号，避免误伤品名里的 x
    text = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", _CROSS_CANON, text)
    # 乘号两侧空格收掉：340g * 24 → 340g*24
    text = re.sub(r"\s*\*\s*", _CROSS_CANON, text)
    return text


def normalize_name(value: Any) -> str:
    """品名：NFKC + 合并连续空白为单空格 + strip。保守——不全删空格、不改大小写。"""
    text = _to_halfwidth(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_spec(value: Any) -> str:
    """规格：NFKC + 小写 + 乘号统一 + 全删空格。只统一写法符号，数字一律保留。"""
    text = _to_halfwidth(str(value or "")).lower()
    text = _unify_cross(text)
    return re.sub(r"\s+", "", text).strip()


def normalize_unit(value: Any) -> str:
    """单位字段：NFKC + trim + 小写查别名表。命中同义词→归一；未命中→保留原样。"""
    text = _to_halfwidth(str(value or "")).strip()
    if not text:
        return ""
    return _UNIT_CANON.get(text.lower(), text)


def normalize_category(value: Any) -> str:
    """类别：NFKC + 合并空白 + strip。"""
    text = _to_halfwidth(str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def normalize_sku(
    name: Any = "",
    spec: Any = "",
    unit: Any = "",
    category: Any = "",
) -> dict[str, str]:
    """把一条 SKU 的四个字段归一为标准写法。返回 {name, spec, unit, category}。"""
    return {
        "name": normalize_name(name),
        "spec": normalize_spec(spec),
        "unit": normalize_unit(unit),
        "category": normalize_category(category),
    }


__all__ = [
    "normalize_name",
    "normalize_spec",
    "normalize_unit",
    "normalize_category",
    "normalize_sku",
]
