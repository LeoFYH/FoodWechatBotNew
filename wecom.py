"""wecom.py —— 企业微信加解密 + 协议解析层(从 main.py 原样搬出,P8)。

只做"无状态的协议处理":XML 解析、签名计算、回调加解密、消息结构(schema)。
**不碰任何运行态**:不读 env、不持有 access_token 缓存 / SEEN 去重集 / cursor / 锁,不 import main。

铁律(e):回调 token / AES key / corp_id 等密钥与配置留在 main,按关键字参数注入
(token=/aes_key=/corp_id=);encrypt 由 main 把构好的 WXBizMsgCrypt 对象传入(crypto=)。
函数体、SQL 无、日志、HTTPException、加解密算法均与原 main 逐字一致,仅把
全局密钥读取改为参数注入(token 值不变,回调行为不变)。

有状态的 access_token / 去重 / cursor / API 发送(send/media/sync) + crypto 工厂
+ 4 个回调路由,留在 main。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import socket
import struct
import xml.etree.ElementTree as ET
from typing import Any

from Crypto.Cipher import AES
from fastapi import HTTPException
from pydantic import BaseModel


logger = logging.getLogger("wechatclaw")


class WecomMessage(BaseModel):
    msg_type: str
    chat_type: str
    chat_id: str
    msg_id: str
    sender_user_id: str
    sender_name: str
    content: str


class WecomKfEvent(BaseModel):
    token: str
    open_kfid: str
    event: str
    create_time: str


def xml_text(parent: ET.Element, path: str, default: str = "") -> str:
    node = parent.find(path)
    if node is None or node.text is None:
        return default
    return node.text


def build_wecom_text_response_xml(content: str) -> bytes:
    root = ET.Element("xml")
    ET.SubElement(root, "MsgType").text = "text"
    text = ET.SubElement(root, "Text")
    ET.SubElement(text, "Content").text = content
    return ET.tostring(root, encoding="utf-8", method="xml")


def compute_wecom_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    raw = "".join(sorted([token, timestamp, nonce, encrypted]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_wecom_plain_message(plain_xml: bytes | str) -> WecomMessage:
    if isinstance(plain_xml, bytes):
        plain_xml = plain_xml.decode("utf-8")

    root = ET.fromstring(plain_xml)
    msg_type = xml_text(root, "MsgType")
    chat_type = xml_text(root, "ChatType")
    chat_id = xml_text(root, "ChatId")
    msg_id = xml_text(root, "MsgId")
    sender_user_id = xml_text(root, "From/UserId")
    sender_name = xml_text(root, "From/Name") or xml_text(root, "From/Alias") or sender_user_id

    if msg_type == "text":
        content = xml_text(root, "Text/Content")
    elif msg_type == "mixed":
        parts: list[str] = []
        mixed = root.find("MixedMessage")
        if mixed is not None:
            for item in mixed:
                if xml_text(item, "MsgType") == "text":
                    parts.append(xml_text(item, "Text/Content"))
        content = "\n".join(part for part in parts if part).strip()
    elif msg_type == "event":
        event_type = xml_text(root, "Event/EventType")
        content = f"[event:{event_type}]"
    else:
        content = ""

    return WecomMessage(
        msg_type=msg_type,
        chat_type=chat_type,
        chat_id=chat_id,
        msg_id=msg_id,
        sender_user_id=sender_user_id,
        sender_name=sender_name,
        content=content,
    )


def encrypt_wecom_response(crypto: Any, content: str, nonce: str, timestamp: str) -> str:
    ret, encrypted_response = crypto.EncryptMsg(
        build_wecom_text_response_xml(content),
        nonce,
        timestamp,
    )
    if ret != 0:
        logger.warning("wecom_encrypt_failed ret=%s", ret)
        raise HTTPException(status_code=500, detail=f"WeCom encrypt failed: {ret}")

    if isinstance(encrypted_response, bytes):
        return encrypted_response.decode("utf-8")
    return encrypted_response


def decrypt_wecom_kf_payload(encrypted: str, *, aes_key: str) -> tuple[bytes, str]:
    if not aes_key:
        raise HTTPException(status_code=500, detail="WECOM_KF_ENCODING_AES_KEY must be configured")

    try:
        key = base64.b64decode(aes_key + "=")
        if len(key) != 32:
            raise ValueError("invalid key length")
        cryptor = AES.new(key, AES.MODE_CBC, key[:16])
        plain_text = cryptor.decrypt(base64.b64decode(encrypted))
        pad = plain_text[-1]
        if pad < 1 or pad > 32:
            raise ValueError("invalid padding")

        content = plain_text[16:-pad]
        if len(content) < 4:
            raise ValueError("missing message length")
        xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
        xml_content = content[4 : xml_len + 4]
        receive_id = content[xml_len + 4 :].decode("utf-8", errors="replace")
        return xml_content, receive_id
    except Exception as exc:
        logger.warning("wecom_kf_decrypt_payload_failed error=%s", exc)
        raise HTTPException(status_code=403, detail="WeCom KF decrypt failed") from exc


def verify_wecom_kf_signature(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    encrypted: str,
    *,
    token: str,
) -> None:
    if not token:
        raise HTTPException(status_code=500, detail="WECOM_KF_CALLBACK_TOKEN must be configured")

    expected_signature = compute_wecom_signature(
        token,
        timestamp,
        nonce,
        encrypted,
    )
    if not hmac.compare_digest(expected_signature, msg_signature):
        logger.warning(
            "wecom_kf_signature_failed expected=%s actual=%s",
            expected_signature,
            msg_signature,
        )
        raise HTTPException(status_code=403, detail="WeCom KF signature failed")


def decrypt_wecom_kf_verify_url(
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
    *,
    token: str,
    aes_key: str,
    corp_id: str,
) -> str:
    verify_wecom_kf_signature(msg_signature, timestamp, nonce, echostr, token=token)
    decrypted_echo, receive_id = decrypt_wecom_kf_payload(echostr, aes_key=aes_key)
    if corp_id and receive_id != corp_id:
        logger.warning(
            "wecom_kf_receive_id_mismatch expected=%s actual=%s",
            corp_id,
            receive_id,
        )
    return decrypted_echo.decode("utf-8")


def decrypt_wecom_kf_message(
    encrypted_body: bytes,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    *,
    token: str,
    aes_key: str,
    corp_id: str,
) -> bytes:
    try:
        encrypted = xml_text(ET.fromstring(encrypted_body), "Encrypt")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid WeCom KF encrypted XML") from exc

    if not encrypted:
        raise HTTPException(status_code=400, detail="Missing WeCom KF Encrypt field")

    verify_wecom_kf_signature(msg_signature, timestamp, nonce, encrypted, token=token)
    plain_xml, receive_id = decrypt_wecom_kf_payload(encrypted, aes_key=aes_key)
    if corp_id and receive_id != corp_id:
        logger.warning(
            "wecom_kf_receive_id_mismatch expected=%s actual=%s",
            corp_id,
            receive_id,
        )
    return plain_xml


def parse_wecom_kf_event(plain_xml: bytes | str) -> WecomKfEvent:
    if isinstance(plain_xml, bytes):
        plain_xml = plain_xml.decode("utf-8")

    root = ET.fromstring(plain_xml)
    msg_type = xml_text(root, "MsgType")
    event = xml_text(root, "Event")

    if msg_type != "event" or event != "kf_msg_or_event":
        raise ValueError(f"Unsupported WeCom KF callback: MsgType={msg_type}, Event={event}")

    return WecomKfEvent(
        token=xml_text(root, "Token"),
        open_kfid=xml_text(root, "OpenKfId"),
        event=event,
        create_time=xml_text(root, "CreateTime"),
    )


__all__ = [
    "WecomMessage",
    "WecomKfEvent",
    "xml_text",
    "build_wecom_text_response_xml",
    "compute_wecom_signature",
    "parse_wecom_plain_message",
    "encrypt_wecom_response",
    "decrypt_wecom_kf_payload",
    "verify_wecom_kf_signature",
    "decrypt_wecom_kf_verify_url",
    "decrypt_wecom_kf_message",
    "parse_wecom_kf_event",
]
