"""routers/wecom.py —— 企业微信回调路由（4 条，路径逐字不变）。

GET/POST /wecom/callback、GET/POST /wecom/kf/callback。
handler 函数体与原 main 逐字一致；依赖经 from main import 引用（main 末尾才 include，无循环）。
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from main import (
    WECOM_KF_CALLBACK_TOKEN,
    WECOM_KF_CORP_ID,
    WECOM_KF_ENCODING_AES_KEY,
    answer_wecom_message,
    decrypt_wecom_kf_message,
    decrypt_wecom_kf_verify_url,
    encrypt_wecom_response,
    get_wecom_crypto,
    is_duplicate_wecom_message,
    logger,
    parse_wecom_kf_event,
    parse_wecom_plain_message,
    process_wecom_kf_event,
    request_client_host,
    request_query_keys,
    require_query_param,
)

router = APIRouter()


@router.get("/wecom/callback")
async def wecom_verify(request: Request):
    logger.info(
        "wecom_verify_hit client=%s query_keys=%s",
        request_client_host(request),
        request_query_keys(request),
    )
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    echostr = require_query_param(request, "echostr")

    ret, decrypted_echo = get_wecom_crypto().VerifyURL(
        msg_signature,
        timestamp,
        nonce,
        echostr,
    )
    if ret != 0:
        logger.warning("wecom_verify_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom verify failed: {ret}")

    if isinstance(decrypted_echo, bytes):
        decrypted_echo = decrypted_echo.decode("utf-8")

    return PlainTextResponse(decrypted_echo)


@router.post("/wecom/callback")
async def wecom_callback(request: Request):
    encrypted_body = await request.body()
    logger.info(
        "wecom_callback_hit client=%s query_keys=%s body_bytes=%s",
        request_client_host(request),
        request_query_keys(request),
        len(encrypted_body),
    )
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")

    ret, plain_xml = get_wecom_crypto().DecryptMsg(
        encrypted_body,
        msg_signature,
        timestamp,
        nonce,
    )
    if ret != 0:
        logger.warning("wecom_decrypt_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom decrypt failed: {ret}")

    message = parse_wecom_plain_message(plain_xml)
    logger.info(
        "wecom_message msg_id=%s msg_type=%s chat_type=%s chat_id=%s sender=%s",
        message.msg_id,
        message.msg_type,
        message.chat_type,
        message.chat_id,
        message.sender_user_id,
    )

    if is_duplicate_wecom_message(message.msg_id):
        logger.info("wecom_duplicate_message msg_id=%s", message.msg_id)
        answer = ""
    else:
        try:
            answer = answer_wecom_message(message)
        except Exception as exc:
            logger.warning("wecom_answer_failed msg_id=%s error=%s", message.msg_id, exc)
            answer = "这条消息处理失败了，请稍后再试。"

    encrypted_response = encrypt_wecom_response(get_wecom_crypto(), answer, nonce, timestamp)
    return PlainTextResponse(encrypted_response, media_type="application/xml")


@router.get("/wecom/kf/callback")
async def wecom_kf_verify(request: Request):
    logger.info(
        "wecom_kf_verify_hit client=%s query_keys=%s",
        request_client_host(request),
        request_query_keys(request),
    )
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    echostr = require_query_param(request, "echostr")

    decrypted_echo = decrypt_wecom_kf_verify_url(
        msg_signature,
        timestamp,
        nonce,
        echostr,
        token=WECOM_KF_CALLBACK_TOKEN,
        aes_key=WECOM_KF_ENCODING_AES_KEY,
        corp_id=WECOM_KF_CORP_ID,
    )

    return PlainTextResponse(decrypted_echo)


@router.post("/wecom/kf/callback")
async def wecom_kf_callback(request: Request, background_tasks: BackgroundTasks):
    encrypted_body = await request.body()
    logger.info(
        "wecom_kf_callback_hit client=%s query_keys=%s body_bytes=%s",
        request_client_host(request),
        request_query_keys(request),
        len(encrypted_body),
    )
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")

    plain_xml = decrypt_wecom_kf_message(
        encrypted_body,
        msg_signature,
        timestamp,
        nonce,
        token=WECOM_KF_CALLBACK_TOKEN,
        aes_key=WECOM_KF_ENCODING_AES_KEY,
        corp_id=WECOM_KF_CORP_ID,
    )

    try:
        event = parse_wecom_kf_event(plain_xml)
    except Exception as exc:
        logger.warning("wecom_kf_parse_failed error=%s", exc)
        return PlainTextResponse("success")

    logger.info(
        "wecom_kf_callback event=%s open_kfid=%s create_time=%s",
        event.event,
        event.open_kfid,
        event.create_time,
    )
    background_tasks.add_task(process_wecom_kf_event, event)
    return PlainTextResponse("success")
