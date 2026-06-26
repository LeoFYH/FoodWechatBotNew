"""llm_json.py —— 从大模型输出里稳健提取 JSON 对象的通用 helper（从 main.py 原样搬出）。

跨切面工具:vision 识别、业务意图/分诊等所有"让模型输出 JSON 再解析"的地方都用它。
纯函数,只依赖标准库,绝不 import main,可被 main.py 通过 `from llm_json import *` 门面 re-export。

- extract_json_object: 解析失败时抛 ValueError(严格场景)。
- safe_extract_json_object: 解析失败时返回 {}(宽松场景,不抛)。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("missing JSON object")

    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def safe_extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


__all__ = [
    "extract_json_object",
    "safe_extract_json_object",
]
