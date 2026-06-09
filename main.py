import json
import logging
import os
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
import httpx
from openai import OpenAI
from pydantic import BaseModel, Field
from wx_crypt import WXBizMsgCrypt, WxChannel_Wecom


load_dotenv()

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_NAME = "deepseek-chat"
DEFAULT_MAX_HISTORY_MESSAGES = 20
DEFAULT_SYSTEM_PROMPT = "你是一个运行在微信里的 AI 助手，回答要简洁、有帮助。"
DEFAULT_WECOM_BOT_NAME = "食品厂机器人"
DEFAULT_WECOM_KF_SYNC_LIMIT = 100
DEFAULT_HTTP_TIMEOUT_SECONDS = 20


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed = int(value)
    except ValueError:
        logging.warning("Invalid %s=%s, using default=%s", name, value, default)
        return default

    if parsed < 1:
        logging.warning("Invalid %s=%s, using default=%s", name, value, default)
        return default

    return parsed


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("wechatclaw")

app = FastAPI(title="WechatClaw Phase 0.5 AI Backend MVP")

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
MODEL_NAME = os.getenv("MODEL_NAME") or DEFAULT_MODEL_NAME
MAX_HISTORY_MESSAGES = get_int_env("MAX_HISTORY_MESSAGES", DEFAULT_MAX_HISTORY_MESSAGES)
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
MEMORY_FILE = Path(os.getenv("MEMORY_FILE", "memory.json"))
MEMORY_LOCK = Lock()
WECOM_CALLBACK_TOKEN = os.getenv("WECOM_CALLBACK_TOKEN") or os.getenv("WX_BOT_TOKEN")
WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY") or os.getenv("WX_BOT_AES_KEY")
WECOM_CORP_ID = os.getenv("WECOM_CORP_ID") or os.getenv("WX_BOT_CORP_ID", "")
WECOM_BOT_NAME = os.getenv("WECOM_BOT_NAME", DEFAULT_WECOM_BOT_NAME)
WECOM_KF_CORP_ID = os.getenv("WECOM_KF_CORP_ID") or WECOM_CORP_ID
WECOM_KF_SECRET = os.getenv("WECOM_KF_SECRET")
WECOM_KF_CALLBACK_TOKEN = os.getenv("WECOM_KF_CALLBACK_TOKEN")
WECOM_KF_ENCODING_AES_KEY = os.getenv("WECOM_KF_ENCODING_AES_KEY")
WECOM_KF_CURSOR_FILE = Path(os.getenv("WECOM_KF_CURSOR_FILE", "kf_cursors.json"))
WECOM_KF_SYNC_LIMIT = get_int_env("WECOM_KF_SYNC_LIMIT", DEFAULT_WECOM_KF_SYNC_LIMIT)
HTTP_TIMEOUT_SECONDS = get_int_env("HTTP_TIMEOUT_SECONDS", DEFAULT_HTTP_TIMEOUT_SECONDS)
SEEN_WECOM_MSG_IDS: set[str] = set()
SEEN_WECOM_MSG_IDS_LOCK = Lock()
SEEN_WECOM_KF_MSG_IDS: set[str] = set()
SEEN_WECOM_KF_MSG_IDS_LOCK = Lock()
WECOM_KF_CURSOR_LOCK = Lock()
WECOM_KF_ACCESS_TOKEN_LOCK = Lock()
WECOM_KF_ACCESS_TOKEN = ""
WECOM_KF_ACCESS_TOKEN_EXPIRES_AT = 0.0

if not LLM_API_KEY:
    raise RuntimeError("LLM_API_KEY is missing. Check your .env file.")

client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
)


class ChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    user_id: str
    answer: str
    history_length: int


class MemoryLengthResponse(BaseModel):
    user_id: str
    history_length: int


class DeleteMemoryResponse(BaseModel):
    deleted: bool
    user_id: str


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


def load_memory() -> dict:
    if not MEMORY_FILE.exists():
        return {}

    raw_memory = MEMORY_FILE.read_text(encoding="utf-8").strip()
    if not raw_memory:
        return {}

    try:
        data = json.loads(raw_memory)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="memory.json is not valid JSON") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="memory.json must contain a JSON object")

    return data


def save_memory(memory: dict) -> None:
    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    return history[-MAX_HISTORY_MESSAGES:]


def call_llm(user_id: str, history: list[dict[str, str]]) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
        )
        answer = response.choices[0].message.content
    except Exception as exc:
        print("========== LLM ERROR ==========")
        print(str(exc))
        traceback.print_exc()
        print("================================")
        logger.warning(
            "llm_failed user_id=%s history_length=%s model=%s error_type=%s",
            user_id,
            len(history),
            MODEL_NAME,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "LLM request failed",
                "real_error": str(exc),
            },
        ) from exc

    logger.info(
        "llm_success user_id=%s history_length=%s model=%s",
        user_id,
        len(history),
        MODEL_NAME,
    )
    return answer or ""


def handle_user_message(user_id: str, message: str) -> ChatResponse:
    user_id = user_id.strip()
    message = message.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")
    if not message:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.setdefault(user_id, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

        history.append({"role": "user", "content": message})
        history = trim_history(history)
        memory[user_id] = history

        logger.info("chat_request user_id=%s history_length=%s", user_id, len(history))

        answer = call_llm(user_id, history)
        history.append({"role": "assistant", "content": answer})
        history = trim_history(history)
        memory[user_id] = history
        save_memory(memory)

    return ChatResponse(
        user_id=user_id,
        answer=answer,
        history_length=len(history),
    )


def get_wecom_crypto() -> WXBizMsgCrypt:
    if not WECOM_CALLBACK_TOKEN or not WECOM_ENCODING_AES_KEY:
        raise HTTPException(
            status_code=500,
            detail="WECOM_CALLBACK_TOKEN and WECOM_ENCODING_AES_KEY must be configured",
        )

    return WXBizMsgCrypt(
        WECOM_CALLBACK_TOKEN,
        WECOM_ENCODING_AES_KEY,
        WECOM_CORP_ID,
        channel=WxChannel_Wecom,
    )


def require_query_param(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise HTTPException(status_code=400, detail=f"missing query parameter: {name}")
    return value


def xml_text(parent: ET.Element, path: str, default: str = "") -> str:
    node = parent.find(path)
    if node is None or node.text is None:
        return default
    return node.text


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


def strip_bot_mention(content: str) -> str:
    content = content.strip()
    if WECOM_BOT_NAME:
        content = content.replace(f"@{WECOM_BOT_NAME}", "").strip()
    return content


def build_wecom_text_response_xml(content: str) -> bytes:
    root = ET.Element("xml")
    ET.SubElement(root, "MsgType").text = "text"
    text = ET.SubElement(root, "Text")
    ET.SubElement(text, "Content").text = content
    return ET.tostring(root, encoding="utf-8", method="xml")


def encrypt_wecom_response(content: str, nonce: str, timestamp: str) -> str:
    ret, encrypted_response = get_wecom_crypto().EncryptMsg(
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


def is_duplicate_wecom_message(msg_id: str) -> bool:
    if not msg_id:
        return False

    with SEEN_WECOM_MSG_IDS_LOCK:
        if msg_id in SEEN_WECOM_MSG_IDS:
            return True
        SEEN_WECOM_MSG_IDS.add(msg_id)
        if len(SEEN_WECOM_MSG_IDS) > 1000:
            for old_msg_id in list(SEEN_WECOM_MSG_IDS)[:200]:
                SEEN_WECOM_MSG_IDS.discard(old_msg_id)
        return False


def answer_wecom_message(message: WecomMessage) -> str:
    if message.msg_type == "event":
        return "我已加入，可以在群里 @我 提问。"

    if message.msg_type not in {"text", "mixed"}:
        return "目前我先支持文字消息，图片、文件后面再接。"

    content = strip_bot_mention(message.content)
    if not content:
        return "我在，直接说你的问题。"

    if message.chat_type == "group":
        memory_user_id = f"group:{message.chat_id}"
        llm_message = f"{message.sender_name}: {content}"
    else:
        memory_user_id = f"user:{message.sender_user_id or message.chat_id}"
        llm_message = content

    return handle_user_message(memory_user_id, llm_message).answer


def get_wecom_kf_crypto() -> WXBizMsgCrypt:
    if not WECOM_KF_CORP_ID:
        raise HTTPException(status_code=500, detail="WECOM_KF_CORP_ID must be configured")
    if not WECOM_KF_CALLBACK_TOKEN or not WECOM_KF_ENCODING_AES_KEY:
        raise HTTPException(
            status_code=500,
            detail="WECOM_KF_CALLBACK_TOKEN and WECOM_KF_ENCODING_AES_KEY must be configured",
        )

    return WXBizMsgCrypt(
        WECOM_KF_CALLBACK_TOKEN,
        WECOM_KF_ENCODING_AES_KEY,
        WECOM_KF_CORP_ID,
        channel=WxChannel_Wecom,
    )


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


def get_wecom_kf_access_token() -> str:
    global WECOM_KF_ACCESS_TOKEN, WECOM_KF_ACCESS_TOKEN_EXPIRES_AT

    now = time.time()
    with WECOM_KF_ACCESS_TOKEN_LOCK:
        if WECOM_KF_ACCESS_TOKEN and WECOM_KF_ACCESS_TOKEN_EXPIRES_AT > now + 60:
            return WECOM_KF_ACCESS_TOKEN

        if not WECOM_KF_CORP_ID or not WECOM_KF_SECRET:
            raise RuntimeError("WECOM_KF_CORP_ID and WECOM_KF_SECRET must be configured")

        response = httpx.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": WECOM_KF_CORP_ID,
                "corpsecret": WECOM_KF_SECRET,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom gettoken failed: {data}")

        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"WeCom gettoken missing access_token: {data}")

        WECOM_KF_ACCESS_TOKEN = access_token
        WECOM_KF_ACCESS_TOKEN_EXPIRES_AT = now + int(data.get("expires_in", 7200)) - 300
        return WECOM_KF_ACCESS_TOKEN


def load_kf_cursors() -> dict[str, str]:
    if not WECOM_KF_CURSOR_FILE.exists():
        return {}

    raw_cursors = WECOM_KF_CURSOR_FILE.read_text(encoding="utf-8").strip()
    if not raw_cursors:
        return {}

    data = json.loads(raw_cursors)
    if not isinstance(data, dict):
        logger.warning("kf_cursor_file_invalid path=%s", WECOM_KF_CURSOR_FILE)
        return {}

    return {str(key): str(value) for key, value in data.items() if value is not None}


def save_kf_cursors(cursors: dict[str, str]) -> None:
    WECOM_KF_CURSOR_FILE.write_text(
        json.dumps(cursors, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_kf_cursor(open_kfid: str) -> str:
    if not open_kfid:
        return ""

    with WECOM_KF_CURSOR_LOCK:
        return load_kf_cursors().get(open_kfid, "")


def set_kf_cursor(open_kfid: str, cursor: str) -> None:
    if not open_kfid or not cursor:
        return

    with WECOM_KF_CURSOR_LOCK:
        cursors = load_kf_cursors()
        cursors[open_kfid] = cursor
        save_kf_cursors(cursors)


def is_duplicate_wecom_kf_message(msg_id: str) -> bool:
    if not msg_id:
        return False

    with SEEN_WECOM_KF_MSG_IDS_LOCK:
        if msg_id in SEEN_WECOM_KF_MSG_IDS:
            return True
        SEEN_WECOM_KF_MSG_IDS.add(msg_id)
        if len(SEEN_WECOM_KF_MSG_IDS) > 5000:
            for old_msg_id in list(SEEN_WECOM_KF_MSG_IDS)[:1000]:
                SEEN_WECOM_KF_MSG_IDS.discard(old_msg_id)
        return False


def post_wecom_kf_api(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    access_token = get_wecom_kf_access_token()
    response = httpx.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/{path}",
        params={"access_token": access_token},
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"WeCom API {path} failed: {data}")
    return data


def send_wecom_kf_text(open_kfid: str, external_userid: str, content: str) -> None:
    if not open_kfid or not external_userid:
        raise RuntimeError("open_kfid and external_userid are required to send WeCom KF message")

    post_wecom_kf_api(
        "kf/send_msg",
        {
            "touser": external_userid,
            "open_kfid": open_kfid,
            "msgtype": "text",
            "text": {"content": content},
        },
    )
    logger.info("wecom_kf_send_success open_kfid=%s external_userid=%s", open_kfid, external_userid)


def handle_wecom_kf_sync_item(item: dict[str, Any]) -> None:
    msg_id = str(item.get("msgid") or "")
    if is_duplicate_wecom_kf_message(msg_id):
        logger.info("wecom_kf_duplicate_message msg_id=%s", msg_id)
        return

    msg_type = item.get("msgtype")
    open_kfid = str(item.get("open_kfid") or item.get("event", {}).get("open_kfid") or "")
    external_userid = str(item.get("external_userid") or item.get("event", {}).get("external_userid") or "")

    logger.info(
        "wecom_kf_sync_item msg_id=%s msg_type=%s open_kfid=%s external_userid=%s origin=%s",
        msg_id,
        msg_type,
        open_kfid,
        external_userid,
        item.get("origin"),
    )

    if msg_type == "event":
        logger.info("wecom_kf_event msg_id=%s event=%s", msg_id, item.get("event", {}))
        return

    if msg_type != "text":
        if open_kfid and external_userid:
            send_wecom_kf_text(open_kfid, external_userid, "目前我先支持文字消息，图片、文件后面再接。")
        return

    content = str(item.get("text", {}).get("content") or "").strip()
    if not content or not open_kfid or not external_userid:
        logger.info("wecom_kf_text_skipped msg_id=%s", msg_id)
        return

    session_id = f"kf:{open_kfid}:{external_userid}"
    answer = handle_user_message(session_id, content).answer
    send_wecom_kf_text(open_kfid, external_userid, answer)


def sync_wecom_kf_messages(event: WecomKfEvent) -> None:
    if not event.token or not event.open_kfid:
        logger.warning("wecom_kf_event_missing_token_or_open_kfid event=%s", event.model_dump())
        return

    cursor = get_kf_cursor(event.open_kfid)

    for _ in range(20):
        payload = {
            "cursor": cursor,
            "token": event.token,
            "limit": WECOM_KF_SYNC_LIMIT,
            "open_kfid": event.open_kfid,
        }
        data = post_wecom_kf_api("kf/sync_msg", payload)
        next_cursor = str(data.get("next_cursor") or "")
        if next_cursor:
            set_kf_cursor(event.open_kfid, next_cursor)
            cursor = next_cursor

        for item in data.get("msg_list", []):
            if isinstance(item, dict):
                handle_wecom_kf_sync_item(item)

        if int(data.get("has_more", 0)) != 1:
            return

    logger.warning("wecom_kf_sync_stopped_after_page_limit open_kfid=%s", event.open_kfid)


def process_wecom_kf_event(event: WecomKfEvent) -> None:
    try:
        sync_wecom_kf_messages(event)
    except Exception as exc:
        logger.exception("wecom_kf_process_failed open_kfid=%s error=%s", event.open_kfid, exc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/wecom/callback")
async def wecom_verify(request: Request):
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


@app.post("/wecom/callback")
async def wecom_callback(request: Request):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    encrypted_body = await request.body()

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

    encrypted_response = encrypt_wecom_response(answer, nonce, timestamp)
    return PlainTextResponse(encrypted_response, media_type="application/xml")


@app.get("/wecom/kf/callback")
async def wecom_kf_verify(request: Request):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    echostr = require_query_param(request, "echostr")

    ret, decrypted_echo = get_wecom_kf_crypto().VerifyURL(
        msg_signature,
        timestamp,
        nonce,
        echostr,
    )
    if ret != 0:
        logger.warning("wecom_kf_verify_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom KF verify failed: {ret}")

    if isinstance(decrypted_echo, bytes):
        decrypted_echo = decrypted_echo.decode("utf-8")

    return PlainTextResponse(decrypted_echo)


@app.post("/wecom/kf/callback")
async def wecom_kf_callback(request: Request, background_tasks: BackgroundTasks):
    msg_signature = require_query_param(request, "msg_signature")
    timestamp = require_query_param(request, "timestamp")
    nonce = require_query_param(request, "nonce")
    encrypted_body = await request.body()

    ret, plain_xml = get_wecom_kf_crypto().DecryptMsg(
        encrypted_body,
        msg_signature,
        timestamp,
        nonce,
    )
    if ret != 0:
        logger.warning("wecom_kf_decrypt_failed ret=%s", ret)
        raise HTTPException(status_code=403, detail=f"WeCom KF decrypt failed: {ret}")

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


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    return handle_user_message(payload.user_id, payload.message)


@app.get("/memory/{user_id}", response_model=MemoryLengthResponse)
def get_memory_length(user_id: str) -> MemoryLengthResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        history = memory.get(user_id, [])

        if not isinstance(history, list):
            raise HTTPException(status_code=500, detail=f"Invalid history for user_id: {user_id}")

    return MemoryLengthResponse(user_id=user_id, history_length=len(history))


@app.delete("/memory/{user_id}", response_model=DeleteMemoryResponse)
def delete_memory(user_id: str) -> DeleteMemoryResponse:
    user_id = user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id cannot be empty")

    with MEMORY_LOCK:
        memory = load_memory()
        memory.pop(user_id, None)
        save_memory(memory)

    logger.info("memory_deleted user_id=%s", user_id)
    return DeleteMemoryResponse(deleted=True, user_id=user_id)
