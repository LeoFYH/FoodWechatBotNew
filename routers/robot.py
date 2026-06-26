"""routers/robot.py —— 机器人/数据 API 路由（路径逐字不变）。

POST /chat、GET /api/orders、POST /api/orders/mark_fetched、POST /api/orders/unmark、
GET /api/receipts、POST /api/receipts/mark_fetched、POST /api/receipts/unmark、
POST /api/orders/import/excel、POST /api/orders/import/photo、POST /api/orders/import/text。
handler 函数体与原 main 逐字一致；依赖经 from main import 引用。
"""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from main import (
    ORDER_STATUS_ALL,
    ORDER_STATUS_NEW,
    ORDER_STATUSES,
    RECEIPT_API_STATUSES,
    RECEIPT_STATUS_NEW,
    ChatRequest,
    ChatResponse,
    IdsRequest,
    MarkFetchedRequest,
    TextOrderImportRequest,
    handle_user_message,
    insert_order_payload,
    llm_order_draft_from_message,
    llm_parse_photo_order,
    logger,
    mark_order_payloads_fetched,
    mark_receipt_payloads_fetched,
    normalize_order_payload,
    order_draft_missing_fields,
    parse_ids_param,
    query_order_payloads,
    query_receipt_payloads_by_status,
    require_robot_api_token,
    save_excel_order_payloads,
    save_order_draft,
    unmark_order_payloads,
    unmark_receipt_payloads,
    validate_iso_date_param,
    vision_client,
    VISION_MODEL,
)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    return handle_user_message(payload.user_id, payload.message)


@router.get("/api/orders")
def api_orders(
    request: Request,
    status: str = Query(default=ORDER_STATUS_NEW),
    order_date: str | None = Query(default=None),
    ids: str | None = Query(default=None),
) -> dict[str, list[dict[str, Any]]]:
    require_robot_api_token(request)
    parsed_ids = parse_ids_param(ids)
    if parsed_ids:
        return {"orders": query_order_payloads(ids=parsed_ids)}

    if status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="status must be new, fetched, or all")
    normalized_order_date = validate_iso_date_param(order_date or "", "order_date")
    query_status = None if status == ORDER_STATUS_ALL else status
    return {"orders": query_order_payloads(status=query_status, order_date=normalized_order_date)}


@router.post("/api/orders/mark_fetched")
def api_mark_fetched(request: Request, payload: MarkFetchedRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    return mark_order_payloads_fetched(payload.ids)


@router.post("/api/orders/unmark")
def api_unmark_orders(request: Request, payload: MarkFetchedRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    return unmark_order_payloads(payload.ids)


@router.get("/api/receipts")
def api_receipts(
    request: Request,
    date: str = Query(...),
    status: str = Query(default=RECEIPT_STATUS_NEW),
) -> dict[str, list[dict[str, Any]]]:
    require_robot_api_token(request)
    if status not in RECEIPT_API_STATUSES:
        raise HTTPException(status_code=400, detail="status must be new, fetched, or all")
    normalized_date = validate_iso_date_param(date, "date")
    return {"receipts": query_receipt_payloads_by_status(normalized_date, status)}


@router.post("/api/receipts/mark_fetched")
def api_mark_receipts_fetched(request: Request, payload: IdsRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    return mark_receipt_payloads_fetched(payload.ids)


@router.post("/api/receipts/unmark")
def api_unmark_receipts(request: Request, payload: IdsRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    return unmark_receipt_payloads(payload.ids)


@router.post("/api/orders/import/excel")
async def api_import_excel(
    request: Request,
    file: UploadFile = File(...),
    raw_ref: str | None = Query(default=None),
) -> dict[str, Any]:
    require_robot_api_token(request)
    file_bytes = await file.read()
    reference = raw_ref or file.filename or "api:excel"
    try:
        saved = save_excel_order_payloads(file_bytes, reference)
    except Exception as exc:
        logger.warning("api_excel_import_failed raw_ref=%s error=%s", reference, exc)
        raise HTTPException(status_code=400, detail="Excel order import failed") from exc
    return {"orders": saved}


@router.post("/api/orders/import/photo")
async def api_import_photo(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Query(default="api"),
    confirm: bool = Query(default=False),
    raw_ref: str | None = Query(default=None),
) -> dict[str, Any]:
    require_robot_api_token(request)
    image_bytes = await file.read()
    reference = raw_ref or file.filename or "api:photo"
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    try:
        draft = llm_parse_photo_order(vision_client, VISION_MODEL, image_bytes, mime_type, reference)
    except Exception as exc:
        logger.warning("api_photo_import_failed raw_ref=%s error=%s", reference, exc)
        if "vision model is not configured" in str(exc):
            raise HTTPException(status_code=503, detail="Photo recognition vision model is not configured") from exc
        raise HTTPException(status_code=400, detail="Photo order parse failed") from exc

    if confirm:
        missing = order_draft_missing_fields(draft)
        if missing:
            raise HTTPException(status_code=400, detail="missing fields: " + "、".join(missing))
        draft["confirmed"] = True
        saved = insert_order_payload(draft)
        return {"order": saved}

    save_order_draft(user_id, draft)
    return {"draft": draft, "message": "photo parsed; confirm before it is visible from /api/orders"}


@router.post("/api/orders/import/text")
def api_import_text(request: Request, payload: TextOrderImportRequest) -> dict[str, Any]:
    require_robot_api_token(request)
    raw_ref = payload.raw_ref or f"api:text:{payload.user_id}"
    draft = llm_order_draft_from_message({}, payload.message)
    if not draft:
        logger.warning("api_text_import_failed user_id=%s", payload.user_id)
        raise HTTPException(status_code=400, detail="Text order parse failed")

    draft["raw_ref"] = raw_ref
    draft["raw_text"] = draft.get("raw_text") or payload.message
    draft["confirmed"] = bool(payload.confirm)
    if payload.confirm:
        missing = order_draft_missing_fields(draft)
        if missing:
            raise HTTPException(status_code=400, detail="missing fields: " + "、".join(missing))
        saved = insert_order_payload(draft)
        return {"order": saved}

    save_order_draft(payload.user_id, draft)
    return {"draft": normalize_order_payload(draft), "message": "text parsed; confirm before it is visible from /api/orders"}
