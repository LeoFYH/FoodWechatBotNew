"""sku_calibrate.py —— 入库 SKU 校准纯函数层。

把照片识别出的潦草入库行，对到 products 字典里的标准 SKU：
- 匹配上：用字典标准 name/spec 替换识别值，并写入标准 code 作匹配标记；
  qty、unit 绝对保留照片识别值（这次入库的真实计量，字典不该有、绝不覆盖）。
- 匹配不上（候选空 / 相似度低 / LLM 弃权 / LLM 返回候选外 code / LLM 失败）：
  一律按未匹配兜底，保留原识别值、code 留空作⚠标记——但该行仍在草稿里、仍照常写库。
- LLM 二次设闸：apply_calibration 校验 LLM 返回的 code 必须真在该行候选集合内，
  编造的 / 不在候选里的一律当未匹配。

纯函数 + 注入式编排：find_candidates / judge 由调用方注入，可独立测试，不碰 DB / LLM / main。
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Callable


# 品名硬闸：LLM 选定后，代码再算"识别名 vs 候选标准名(只比 name，不认别名)"的相似度，
# 低于此值一律推翻判未匹配。把"绝不乱配"从求 LLM 听话变成代码强制。
ACCEPT_SCORE = 0.7


_PROMPT_HEADER = """\
你是入库 SKU 校准助手。下面每一行是照片识别出的入库成品（可能潦草 / 有错别字），
每行附带从标准 SKU 字典粗筛出的候选。请判断每一行对应哪个候选，或都不对。

只输出 JSON，不要解释，不要 Markdown：
{"matches":[{"index":0,"code":"sku_xxx"},{"index":1,"code":null}]}

规则（从严，宁可不配也绝不错配）：
- 只有当识别品名与某候选品名【几乎完全一致】（仅个别错别字、同义写法差异）时，才填该候选的 code。
- 凡是字面差异较大、语义拿不准、只是部分词重叠、或像是不同成品 → 一律填 null。
- 例：识别"冷冻熟鸭蛋面"与候选"黄豆油焖鸡"字面差很多，绝不能配；这种一律 null。
- 只能填候选里出现过的 code，绝不编造。
- 数量不参与判断。拿不准就 null。"""


def _name_score(recognized: str, standard: str) -> float:
    recognized = str(recognized or "").strip()
    standard = str(standard or "").strip()
    if not recognized or not standard:
        return 0.0
    return SequenceMatcher(None, recognized, standard).ratio()


def build_calibration_prompt(
    items: list[dict[str, Any]],
    candidates_by_index: dict[int, list[dict[str, Any]]],
) -> str:
    """把"识别行 + 各自候选"拼成给 LLM 的精判 prompt。纯函数。"""
    blocks: list[str] = []
    for index, raw in enumerate(items):
        item = raw if isinstance(raw, dict) else {}
        qty = item.get("qty")
        qty_text = "?" if qty is None else str(qty)
        recognized = (
            f"[{index}] 识别：{item.get('name') or '?'}"
            f" / 规格：{item.get('spec') or '-'}"
            f" / {qty_text}{item.get('unit') or ''}"
        )
        candidates = candidates_by_index.get(index) or []
        if candidates:
            cand_lines = [
                f"      code={cand.get('code')} 名={cand.get('name')} 规格={cand.get('spec') or '-'}"
                for cand in candidates
            ]
            cand_text = "    候选：\n" + "\n".join(cand_lines)
        else:
            cand_text = "    候选：（无）"
        blocks.append(recognized + "\n" + cand_text)
    return _PROMPT_HEADER + "\n\n待校准：\n" + "\n".join(blocks)


def normalize_matches(parsed: Any) -> dict[int, str | None]:
    """把 LLM 输出的 {"matches":[...]} 解析成 {行号: code|None}。容错：非法结构→空。"""
    matches: dict[int, str | None] = {}
    if not isinstance(parsed, dict):
        return matches
    rows = parsed.get("matches")
    if not isinstance(rows, list):
        return matches
    for row in rows:
        if not isinstance(row, dict):
            continue
        index = row.get("index")
        if not isinstance(index, int):
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
        code = row.get("code")
        code = str(code).strip() if code not in (None, "") else None
        matches[index] = code or None
    return matches


def apply_calibration(
    items: list[dict[str, Any]],
    candidates_by_index: dict[int, list[dict[str, Any]]],
    matches: dict[int, str | None],
    *,
    debug: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """按 LLM 判定改写每行。守铁律：只换 name/spec(+标准 code)，qty/unit 绝不动。

    两道闸：① 二次设闸——LLM 返回的 code 必须真在该行候选内；② 品名硬闸——选定候选的
    标准品名与识别名相似度须 >= ACCEPT_SCORE(只比 name、不认别名)，否则推翻判未匹配。
    未匹配一律保留原值、code 留空作⚠标记。debug 给则逐行回报决策依据。
    """
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        item = dict(raw) if isinstance(raw, dict) else {}
        recognized = str(item.get("name") or "")
        candidates = candidates_by_index.get(index) or []
        code = matches.get(index)

        in_pool: dict[str, Any] | None = None
        if code:
            for cand in candidates:
                if cand.get("code") == code:  # 闸①二次设闸：必须在候选集合内
                    in_pool = cand
                    break

        accepted = False
        reason: str
        gate_score = 0.0
        if not candidates:
            reason = "无候选"
        elif not code:
            reason = "LLM弃权"
        elif in_pool is None:
            reason = "LLM编造/候选外code"
        else:
            gate_score = _name_score(recognized, in_pool.get("name"))
            if gate_score >= ACCEPT_SCORE:  # 闸②品名硬闸
                accepted = True
                reason = f"接受(品名分{round(gate_score, 3)}>={ACCEPT_SCORE})"
            else:
                reason = f"品名硬闸推翻(品名分{round(gate_score, 3)}<{ACCEPT_SCORE})"

        if accepted and in_pool is not None:
            item["name"] = in_pool.get("name") or item.get("name")
            item["spec"] = in_pool.get("spec") or ""  # 用字典标准规格（含空）
            item["code"] = in_pool.get("code")  # 标准 code：匹配标记 + 入库 SKU 关联
            # qty、unit 绝对不动，永远是照片识别值
        else:
            item["code"] = None  # 未匹配：留空作⚠标记；name/spec/qty/unit 原样保留

        if debug is not None:
            cand_text = ", ".join(
                f"{c.get('name')}:{c.get('score')}" for c in candidates
            ) or "(无)"
            verdict = f"✅{item['name']}" if accepted else "⚠未匹配"
            debug(
                f"行{index} 识别='{recognized}' 候选=[{cand_text}] "
                f"LLM选={code} 结果={verdict} 原因={reason}"
            )

        result.append(item)
    return result


def calibrate_receipt_items(
    items: list[dict[str, Any]],
    *,
    find_candidates: Callable[[str], list[dict[str, Any]]],
    judge: Callable[[str], Any],
    debug: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """编排一整张入库草稿的校准：逐行粗筛候选 → 一次 LLM 批量精判 → 改写。

    find_candidates(name)->候选列表；judge(prompt)->LLM 已解析 JSON 对象。
    判 LLM 之前若全行皆无候选则免调 LLM；judge 抛错 → 全行按未匹配兜底。
    debug 给则逐行回报"识别名/候选(名:分)/LLM选/结果/原因"，便于排查侧门混入。
    """
    if not items:
        return items

    candidates_by_index: dict[int, list[dict[str, Any]]] = {}
    for index, raw in enumerate(items):
        item = raw if isinstance(raw, dict) else {}
        name = str(item.get("name") or "").strip()
        candidates_by_index[index] = (find_candidates(name) or []) if name else []

    if not any(candidates_by_index.values()):
        return apply_calibration(items, candidates_by_index, {}, debug=debug)

    prompt = build_calibration_prompt(items, candidates_by_index)
    try:
        parsed = judge(prompt)
    except Exception:
        parsed = None  # LLM 失败 → 全行未匹配兜底，绝不阻断照片流程
    matches = normalize_matches(parsed)
    return apply_calibration(items, candidates_by_index, matches, debug=debug)


__all__ = [
    "build_calibration_prompt",
    "normalize_matches",
    "apply_calibration",
    "calibrate_receipt_items",
]
