from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any

from .db import jsonb


TABLE = "products"

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    return _WHITESPACE_RE.sub(" ", text)


def _sku_signature(name: str, spec: str) -> str:
    return f"{_normalize_text(name)}|{_normalize_text(spec)}"


def generate_product_code(name: str, spec: str = "") -> str:
    """归一化 (name+spec) 生成确定性 code。

    同一个品名+规格永远生成同一个 code，配合 ``on conflict (tenant_id, code)``
    使重复导入天然幂等——无需用户提供 code，也不动 schema 的 unique 约束。
    """
    digest = hashlib.sha256(_sku_signature(name, spec).encode("utf-8")).hexdigest()
    return f"sku_{digest[:12]}"


def _row_to_product(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "code": row.get("code") or "",
        "name": row.get("name") or "",
        "spec": row.get("spec") or "",
        "unit": row.get("unit") or "",
        "category": row.get("category") or "",
        "status": row.get("status") or "",
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
    }


def upsert_product(
    conn: Any,
    tenant_id: int,
    *,
    name: str,
    spec: str = "",
    unit: str = "",
    category: str = "",
    status: str = "active",
    code: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    """插入或更新一个 SKU，按 unique(tenant_id, code) 幂等。

    code 缺省时按归一化 (name+spec) 自动生成，故重复导入只更新、不产生重复行。
    """
    clean_name = _normalize_text(name)
    if not clean_name:
        raise ValueError("upsert_product requires a non-empty name")
    clean_spec = _normalize_text(spec)
    product_code = str(code or "").strip() or generate_product_code(clean_name, clean_spec)
    row = conn.execute(
        """
        insert into products (
            tenant_id, code, name, spec, unit, category, status, metadata
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (tenant_id, code)
        do update set
            name = excluded.name,
            spec = excluded.spec,
            unit = excluded.unit,
            category = excluded.category,
            status = excluded.status,
            metadata = excluded.metadata,
            updated_at = now()
        returning id
        """,
        (
            tenant_id,
            product_code,
            clean_name,
            clean_spec,
            _normalize_text(unit),
            _normalize_text(category),
            _normalize_text(status) or "active",
            jsonb(metadata or {}),
        ),
    ).fetchone()
    return int(row["id"])


def list_active_products(conn: Any, tenant_id: int) -> list[dict[str, Any]]:
    """拉取当前启用（status='active'）的 SKU 清单，供校准用。"""
    rows = conn.execute(
        """
        select id, code, name, spec, unit, category, status, metadata
        from products
        where tenant_id = %s and status = 'active'
        order by name asc, id asc
        """,
        (tenant_id,),
    ).fetchall()
    return [_row_to_product(row) for row in rows]


def _score(query: str, candidate: str) -> float:
    candidate = _normalize_text(candidate)
    if not candidate:
        return 0.0
    return SequenceMatcher(None, query, candidate).ratio()


def find_product_candidates(
    conn: Any,
    tenant_id: int,
    query_name: str,
    top_n: int = 5,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """校准核心：返回与潦草品名最相似的 top_n 候选（同时比对 name 与 product_aliases）。

    只做字符相似度粗筛，返回候选交上层（LLM）精判，本函数不做最终判定。
    """
    query = _normalize_text(query_name)
    if not query:
        return []
    rows = conn.execute(
        """
        select p.id, p.code, p.name, a.alias
        from products p
        left join product_aliases a
            on a.product_id = p.id and a.tenant_id = p.tenant_id
        where p.tenant_id = %s and p.status = 'active'
        """,
        (tenant_id,),
    ).fetchall()

    products: dict[int, dict[str, Any]] = {}
    for row in rows:
        pid = int(row["id"])
        entry = products.get(pid)
        if entry is None:
            entry = {
                "product_id": pid,
                "code": row.get("code") or "",
                "name": row.get("name") or "",
                "aliases": [],
            }
            products[pid] = entry
        alias = row.get("alias")
        if alias:
            entry["aliases"].append(str(alias))

    candidates: list[dict[str, Any]] = []
    for entry in products.values():
        best_score = _score(query, entry["name"])
        matched_on = "name"
        matched_text = entry["name"]
        for alias in entry["aliases"]:
            alias_score = _score(query, alias)
            if alias_score > best_score:
                best_score = alias_score
                matched_on = "alias"
                matched_text = alias
        if best_score < min_score:
            continue
        candidates.append(
            {
                "product_id": entry["product_id"],
                "code": entry["code"],
                "name": entry["name"],
                "matched_on": matched_on,
                "matched_text": matched_text,
                "score": round(best_score, 4),
            }
        )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_n]
