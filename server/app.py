import os
import re
import tempfile
import io
import logging
import json
import secrets
import time
import hmac
import hashlib
import difflib
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import boto3

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SES_FROM_EMAIL = os.getenv("SES_FROM_EMAIL")
DEFAULT_TO_EMAIL = os.getenv("DEFAULT_TO_EMAIL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_SERVER_CLIENT_ID = os.getenv("GOOGLE_SERVER_CLIENT_ID") or GOOGLE_CLIENT_ID
GOOGLE_ANDROID_CLIENT_ID = os.getenv("GOOGLE_ANDROID_CLIENT_ID")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "gptmailer_session")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() != "false"
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"
ALLOWED_GOOGLE_EMAIL = (os.getenv("ALLOWED_GOOGLE_EMAIL") or "").strip().lower()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET")
SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI")
SLACK_USER_SCOPES = os.getenv(
    "SLACK_USER_SCOPES",
    "users:read,im:history,im:write,chat:write,channels:history,groups:history,mpim:history,channels:read,groups:read,mpim:read",
)
SLACK_TOKEN_STORE_PATH = Path(
    os.getenv("SLACK_TOKEN_STORE_PATH", str(Path(__file__).parent / "slack_user_tokens.json"))
)

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and SES_FROM_EMAIL and DEFAULT_TO_EMAIL):
    raise RuntimeError("Missing AWS/SES environment variables")

client = OpenAI()
logger = logging.getLogger(__name__)
sessions: dict[str, dict] = {}
slack_user_tokens: dict[str, dict] = {}

ses = boto3.client(
    "ses",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TTS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
}

SUMMARY_SYSTEM_PROMPT = (
    "You are summarizing a conversation. Return 5-10 concise bullet points. "
    "Capture decisions, requests, and action items. Avoid fluff."
)

CHAT_SYSTEM_PROMPT = (
    "You are a concise, helpful voice assistant in a web app. "
    "Provide direct answers and practical next steps when useful."
)
CHAT_ACTION_SYSTEM_PROMPT = """You are the action planner for a web assistant.

Return exactly one JSON object and no other text. The JSON must match this shape:
{
  "reply": string,
  "action": {
    "type": "none" | "slack_connect" | "slack_read_inbox" | "slack_read_user" | "slack_send_message" | "email_summary" | "confirm_pending" | "cancel_pending",
    "target_name": string,
    "text": string,
    "to": string
  }
}

Rules:
- Use "none" for normal conversational replies with no side effects.
- Use "slack_connect" when the user wants Slack functionality but Slack is not connected.
- Use "slack_read_inbox" to read the user's recent incoming Slack DMs.
- Use "slack_read_user" to read the latest DM from one specific Slack user. Put the user in "target_name".
- Use "slack_send_message" to prepare a Slack DM draft. Put the user in "target_name" and the message body in "text".
- Use "email_summary" when the user wants the conversation summarized and emailed. Put the recipient in "to" if one is given.
- Use "confirm_pending" when the user is clearly confirming a pending action.
- Use "cancel_pending" when the user is clearly canceling a pending action.
- Never claim an email or Slack message was already sent. Python executes actions after you return JSON.
- If a side-effecting action needs confirmation, the reply should say it is a draft or pending confirmation.
- If required info is missing, ask a short follow-up question and use "none".
"""

LAST_MESSAGE_CMD_RE = re.compile(
    r"(?:last|latest)\s+(?:a\s+)?(?:slack\s+)?message\s+from\s+(<@([A-Z0-9]+)>|[^:]+?)\s*$",
    re.IGNORECASE,
)
SLACK_INBOX_CMD_RE = re.compile(
    r"(?:latest|recent)\s+(?:slack\s+)?(?:dm|dms|messages?)\s+(?:to|for)\s+me|(?:read|check)\s+(?:my\s+)?slack",
    re.IGNORECASE,
)
SLACK_REPLY_CMD_RE = re.compile(
    r"^(?:(?:reply|respond)\s+to|(?:send(?:\s+a)?\s+slack\s+message\s+to)|(?:send\s+message\s+to))\s+(<@([A-Z0-9]+)>|[^:]+?)\s*:\s*(.+)$",
    re.IGNORECASE,
)
SLACK_SEND_CONFIRM_RE = re.compile(
    r"^(?:send(?:\s+it|\s+that|\s+now)?|confirm(?:\s+send)?|yes,\s*send|yes\s+send)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
SLACK_SEND_CANCEL_RE = re.compile(
    r"^(?:cancel|don't\s+send|do\s+not\s+send|never\s+mind)\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _load_slack_user_tokens() -> dict[str, dict]:
    if not SLACK_TOKEN_STORE_PATH.exists():
        return {}
    try:
        return json.loads(SLACK_TOKEN_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load Slack token store")
        return {}


def _save_slack_user_tokens() -> None:
    tmp_path = SLACK_TOKEN_STORE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(slack_user_tokens, indent=2), encoding="utf-8")
    tmp_path.replace(SLACK_TOKEN_STORE_PATH)


def _session_user_key(session: dict) -> str | None:
    sub = (session.get("sub") or "").strip()
    if sub:
        return f"sub:{sub}"
    email = (session.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    return None


def _get_slack_user_record(session: dict) -> dict | None:
    key = _session_user_key(session)
    if not key:
        return None
    return slack_user_tokens.get(key)


def _set_slack_user_record(session: dict, record: dict) -> None:
    key = _session_user_key(session)
    if not key:
        raise RuntimeError("Cannot map Slack token: missing session identity")
    slack_user_tokens[key] = record
    _save_slack_user_tokens()


def _delete_slack_user_record(session: dict) -> None:
    key = _session_user_key(session)
    if not key:
        return
    if key in slack_user_tokens:
        slack_user_tokens.pop(key, None)
        _save_slack_user_tokens()


slack_user_tokens = _load_slack_user_tokens()


def _json_post(url: str, form_data: dict) -> dict:
    body = urlencode(form_data).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _json_get(url: str, headers: dict | None = None) -> dict:
    req = Request(url, headers=headers or {}, method="GET")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _bearer_token_from_request(request: FastAPIRequest) -> str | None:
    auth_header = (request.headers.get("authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    return token or None


def _session_id_from_request(request: FastAPIRequest) -> str | None:
    cookie_session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_session_id:
        return cookie_session_id
    return _bearer_token_from_request(request)


def _session_from_request(request: FastAPIRequest) -> dict | None:
    session_id = _session_id_from_request(request)
    if not session_id:
        return None
    entry = sessions.get(session_id)
    if not entry:
        return None
    now = int(time.time())
    if entry.get("expires_at", 0) <= now:
        sessions.pop(session_id, None)
        return None
    return entry


def _require_auth(request: FastAPIRequest) -> dict:
    if not AUTH_REQUIRED:
        return {"sub": "anonymous", "email": "anonymous@local"}
    session = _session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def _google_allowed_client_ids() -> set[str]:
    return {
        value
        for value in (
            GOOGLE_CLIENT_ID,
            GOOGLE_SERVER_CLIENT_ID,
            GOOGLE_ANDROID_CLIENT_ID,
        )
        if value
    }


def _create_session(userinfo: dict, auth_mode: str) -> tuple[str, dict]:
    session_id = secrets.token_urlsafe(32)
    session = {
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
        "auth_mode": auth_mode,
    }
    sessions[session_id] = session
    return session_id, session


def _google_userinfo_from_id_token(id_token: str) -> dict:
    token_data = _json_get(
        f"https://oauth2.googleapis.com/tokeninfo?id_token={quote_plus(id_token)}"
    )
    aud = (token_data.get("aud") or "").strip()
    if aud not in _google_allowed_client_ids():
        raise HTTPException(status_code=403, detail="Google token audience is not allowed")

    email = (token_data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google token did not include an email")
    if str(token_data.get("email_verified")).lower() not in {"true", "1"}:
        raise HTTPException(status_code=403, detail="Google email is not verified")
    if ALLOWED_GOOGLE_EMAIL and email != ALLOWED_GOOGLE_EMAIL:
        raise HTTPException(status_code=403, detail="This Google account is not allowed")

    return {
        "sub": token_data.get("sub"),
        "email": email,
        "name": token_data.get("name") or email,
        "picture": token_data.get("picture"),
    }


def _extract_output_text(resp) -> str:
    if hasattr(resp, "output_text") and resp.output_text:
        return resp.output_text
    try:
        return resp.output[0].content[0].text  # type: ignore[index]
    except Exception:
        return str(resp)


def _verify_slack_signature(request: FastAPIRequest, raw_body: bytes) -> bool:
    if not SLACK_SIGNING_SECRET:
        logger.error("Missing SLACK_SIGNING_SECRET")
        return False

    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")

    if not timestamp or not signature:
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        return False

    if abs(int(time.time()) - ts) > 60 * 5:
        return False

    basestring = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def _slack_api_call(method: str, payload: dict | None = None, token: str | None = None) -> dict:
    effective_token = token or SLACK_BOT_TOKEN
    if not effective_token:
        raise RuntimeError("Missing Slack token")

    req = Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {effective_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error')}")
    return result


def _slack_post_message(
    channel: str, text: str, thread_ts: str | None = None, token: str | None = None
) -> None:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    _slack_api_call("chat.postMessage", payload, token=token)


def _extract_target_user_id(command_text: str) -> str | None:
    match = LAST_MESSAGE_CMD_RE.search(command_text)
    if not match:
        return None
    if match.group(2):
        return match.group(2)
    return None


def _resolve_user_id_from_name(name: str, token: str | None = None) -> str | None:
    normalized = _normalize_slack_name(name)
    if not normalized:
        return None

    best_match: tuple[float, str] | None = None
    second_best_score = 0.0
    cursor = None
    for _ in range(20):
        payload = {"limit": 200}
        if cursor:
            payload["cursor"] = cursor
        data = _slack_api_call("users.list", payload, token=token)
        for member in data.get("members", []):
            profile = member.get("profile", {})
            if member.get("deleted") or member.get("is_bot"):
                continue
            member_id = member.get("id")
            if not member_id:
                continue
            candidates = {
                _normalize_slack_name(member.get("name") or ""),
                _normalize_slack_name(profile.get("display_name") or ""),
                _normalize_slack_name(profile.get("real_name") or ""),
            }
            compact_candidates = {_compact_slack_name(candidate) for candidate in candidates if candidate}
            skeleton_candidates = {_slack_name_skeleton(candidate) for candidate in candidates if candidate}
            if normalized in candidates:
                return member_id
            normalized_compact = _compact_slack_name(normalized)
            if normalized_compact and normalized_compact in compact_candidates:
                return member_id
            normalized_skeleton = _slack_name_skeleton(normalized)
            if normalized_skeleton and normalized_skeleton in skeleton_candidates:
                return member_id
            if any(c.startswith(normalized) for c in candidates if c):
                return member_id
            if normalized_compact and any(
                c.startswith(normalized_compact) for c in compact_candidates if c
            ):
                return member_id
            if normalized_skeleton and any(
                c.startswith(normalized_skeleton) for c in skeleton_candidates if c
            ):
                return member_id
            if any(normalized in c.split() for c in candidates if c):
                return member_id
            score = _score_slack_name_match(normalized, candidates)
            if score > (best_match[0] if best_match else 0.0):
                second_best_score = best_match[0] if best_match else second_best_score
                best_match = (score, member_id)
            elif score > second_best_score:
                second_best_score = score
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if best_match and best_match[0] >= 0.86 and (best_match[0] - second_best_score) >= 0.05:
        return best_match[1]
    return None


def _normalize_slack_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip().lower().lstrip("@").rstrip(".,!?"))
    return cleaned


def _compact_slack_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value or "")


def _slack_name_skeleton(value: str) -> str:
    compacted = _compact_slack_name(value)
    if not compacted:
        return ""
    return compacted[:1] + re.sub(r"[aeiou]", "", compacted[1:])


def _score_slack_name_match(query: str, candidates: set[str]) -> float:
    compact_query = _compact_slack_name(query)
    best = 0.0
    for candidate in candidates:
        if not candidate:
            continue
        compact_candidate = _compact_slack_name(candidate)
        best = max(best, difflib.SequenceMatcher(None, query, candidate).ratio())
        if compact_query and compact_candidate:
            best = max(best, difflib.SequenceMatcher(None, compact_query, compact_candidate).ratio())
    return best


def _parse_last_message_target_user_id(command_text: str, token: str | None = None) -> str | None:
    mention_id = _extract_target_user_id(command_text)
    if mention_id:
        return mention_id

    match = LAST_MESSAGE_CMD_RE.search(command_text)
    if not match:
        return None
    if match.group(2):
        return match.group(2)

    raw_name = match.group(1) or ""
    try:
        return _resolve_user_id_from_name(raw_name, token=token)
    except Exception:
        return None


def _get_last_message_from_user(channel: str, user_id: str, token: str | None = None) -> dict | None:
    cursor = None
    for _ in range(20):
        payload = {"channel": channel, "limit": 200, "inclusive": True}
        if cursor:
            payload["cursor"] = cursor
        data = _slack_api_call("conversations.history", payload, token=token)
        messages = data.get("messages", [])
        for msg in messages:
            if msg.get("user") != user_id:
                continue
            if msg.get("subtype") == "bot_message":
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            return msg
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return None


def _get_last_dm_message_from_user(
    user_id: str, token: str | None = None
) -> tuple[dict | None, str | None]:
    open_data = _slack_api_call("conversations.open", {"users": user_id}, token=token)
    channel = (open_data.get("channel") or {}).get("id")
    if not channel:
        return None, None
    return _get_last_message_from_user(channel, user_id, token=token), channel


def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    cleaned = (text or "").strip()
    if bot_user_id:
        cleaned = cleaned.replace(f"<@{bot_user_id}>", "").strip()
    return cleaned


def _require_slack_oauth_config() -> None:
    if not (SLACK_CLIENT_ID and SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="Slack OAuth is not configured")


def _slack_user_token(session: dict) -> str | None:
    record = _get_slack_user_record(session) or {}
    token = (record.get("access_token") or "").strip()
    if not token:
        return None
    return token


def _slack_user_display_name(user_id: str, token: str, cache: dict[str, str]) -> str:
    if user_id in cache:
        return cache[user_id]
    try:
        data = _slack_api_call("users.info", {"user": user_id}, token=token)
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        display = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception:
        display = user_id
    cache[user_id] = display
    return display


def _clean_requested_name(raw_name: str) -> str:
    return (raw_name or "").strip().lstrip("@").rstrip(".,!?")


def _slack_latest_incoming_dms(token: str, self_user_id: str, limit: int = 5) -> list[dict]:
    channels: list[dict] = []
    cursor = None
    for _ in range(5):
        payload = {"types": "im", "exclude_archived": True, "limit": 200}
        if cursor:
            payload["cursor"] = cursor
        data = _slack_api_call("users.conversations", payload, token=token)
        channels.extend(data.get("channels", []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    incoming: list[dict] = []
    for channel in channels:
        channel_id = channel.get("id")
        if not channel_id:
            continue
        history = _slack_api_call(
            "conversations.history",
            {"channel": channel_id, "limit": 20, "inclusive": True},
            token=token,
        )
        for msg in history.get("messages", []):
            sender = msg.get("user")
            if not sender or sender == self_user_id:
                continue
            if msg.get("subtype") == "bot_message":
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            ts = msg.get("ts") or "0"
            try:
                sort_ts = float(ts)
            except Exception:
                sort_ts = 0.0
            incoming.append(
                {
                    "channel": channel_id,
                    "user": sender,
                    "text": text,
                    "ts": ts,
                    "sort_ts": sort_ts,
                }
            )
            break

    incoming.sort(key=lambda m: m["sort_ts"], reverse=True)
    return incoming[:limit]


def _slack_try_get_permalink(
    channel: str | None, message_ts: str | None, token: str | None = None
) -> str | None:
    if not channel or not message_ts:
        return None
    try:
        data = _slack_api_call(
            "chat.getPermalink",
            {"channel": channel, "message_ts": message_ts},
            token=token,
        )
        return data.get("permalink")
    except Exception as exc:
        logger.warning("Slack permalink unavailable channel=%s ts=%s error=%s", channel, message_ts, exc)
        return None


def _slack_message_exists(channel: str, message_ts: str, token: str) -> bool:
    try:
        data = _slack_api_call(
            "conversations.history",
            {"channel": channel, "latest": message_ts, "inclusive": True, "limit": 5},
            token=token,
        )
        for msg in data.get("messages", []):
            if (msg.get("ts") or "") == message_ts:
                return True
        return False
    except Exception as exc:
        logger.warning(
            "Slack post-send verification failed channel=%s ts=%s error=%s",
            channel,
            message_ts,
            exc,
        )
        return False


def _generate_text(messages: list, model: str = "gpt-4o-mini") -> str:
    if hasattr(client, "responses"):
        resp = client.responses.create(
            model=model,
            input=messages,
        )
        return _extract_output_text(resp).strip()

    completion = client.chat.completions.create(
        model=model,
        messages=messages,
    )
    return (completion.choices[0].message.content or "").strip()


def _extract_email_address(text: str | None) -> str | None:
    if not text:
        return None
    match = EMAIL_RE.search(text)
    if not match:
        return None
    return match.group(0)


def _conversation_text_from_history(history: list[dict]) -> str:
    lines = []
    for item in history:
        if (
            isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
            and item.get("content").strip()
        ):
            role = "Assistant" if item["role"] == "assistant" else "User"
            lines.append(f"{role}: {item['content'].strip()}")
    return "\n".join(lines)


def _pending_action_summary(pending_action: dict | None) -> dict | None:
    if not pending_action or not isinstance(pending_action, dict):
        return None
    action_type = (pending_action.get("type") or "").strip()
    if action_type == "slack_send_message":
        return {
            "type": action_type,
            "target_name": pending_action.get("target_user_label") or "",
            "text": pending_action.get("text") or "",
        }
    if action_type == "email_summary":
        return {
            "type": action_type,
            "to": pending_action.get("to") or "",
        }
    return {"type": action_type}


def _normalize_action_plan(plan: dict, user_message: str) -> dict:
    if not isinstance(plan, dict):
        return {"reply": "", "action": {"type": "none"}}

    reply = (plan.get("reply") or "").strip()
    raw_action = plan.get("action")
    action = raw_action if isinstance(raw_action, dict) else {}
    action_type = (action.get("type") or "none").strip().lower()
    if action_type not in {
        "none",
        "slack_connect",
        "slack_read_inbox",
        "slack_read_user",
        "slack_send_message",
        "email_summary",
        "confirm_pending",
        "cancel_pending",
    }:
        action_type = "none"

    normalized_action = {"type": action_type}
    target_name = (action.get("target_name") or "").strip()
    text = (action.get("text") or "").strip()
    to_email = (action.get("to") or "").strip()

    if action_type in {"slack_read_user", "slack_send_message"} and target_name:
        normalized_action["target_name"] = target_name
    if action_type == "slack_send_message" and text:
        normalized_action["text"] = text
    if action_type == "email_summary":
        normalized_action["to"] = to_email or (_extract_email_address(user_message) or "")

    return {"reply": reply, "action": normalized_action}


def _plan_chat_action(
    user_message: str,
    history: list[dict],
    slack_connected: bool,
    pending_action: dict | None,
) -> dict:
    planning_payload = {
        "message": user_message,
        "history": [
            {"role": item.get("role"), "content": item.get("content")}
            for item in history
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
            and item.get("content").strip()
        ],
        "capabilities": {
            "slack_connected": slack_connected,
            "email_summary_available": True,
        },
        "pending_action": _pending_action_summary(pending_action),
    }
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CHAT_ACTION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(planning_payload)},
            ],
            response_format={"type": "json_object"},
        )
        content = (completion.choices[0].message.content or "").strip()
        return _normalize_action_plan(json.loads(content), user_message)
    except Exception:
        logger.exception("Structured chat action planning failed")
        return {"reply": "", "action": {"type": "none"}}


def _send_summary_email(conversation: str, to_email: str | None) -> dict:
    if not conversation or not isinstance(conversation, str):
        raise HTTPException(status_code=400, detail="Missing conversation text")
    if not isinstance(to_email, str) or not EMAIL_RE.fullmatch(to_email):
        raise HTTPException(status_code=400, detail="Invalid recipient email")

    summary = _generate_text(
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": conversation},
        ],
        model="gpt-4o-mini",
    )
    if not summary:
        raise HTTPException(status_code=500, detail="Empty summary")

    subject = f"Chat summary - {datetime.now(timezone.utc).date().isoformat()}"
    ses.send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": summary}},
        },
    )
    return {"ok": True, "to": to_email, "subject": subject, "summary": summary}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/auth/google/login")
def auth_google_login():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")

    state = secrets.token_urlsafe(24)
    oauth_params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    logger.info(
        "Starting Google OAuth login client_id=%s redirect_uri=%s auth_cookie_secure=%s",
        GOOGLE_CLIENT_ID,
        GOOGLE_REDIRECT_URI,
        AUTH_COOKIE_SECURE,
    )
    params = urlencode(oauth_params)
    response = RedirectResponse(
        url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}", status_code=302
    )
    response.set_cookie(
        "g_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=AUTH_COOKIE_SECURE,
    )
    return response


@app.get("/auth/google/callback")
def auth_google_callback(request: FastAPIRequest, code: str | None = None, state: str | None = None):
    expected_state = request.cookies.get("g_oauth_state")
    logger.info(
        "Google OAuth callback received has_code=%s state_match=%s expected_state_present=%s",
        bool(code),
        bool(state and expected_state and state == expected_state),
        bool(expected_state),
    )
    if not code or not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")

    try:
        token_data = _json_post(
            "https://oauth2.googleapis.com/token",
            {
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        access_token = token_data.get("access_token")
        if not access_token:
            raise RuntimeError("No access token returned")
        userinfo = _json_get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except Exception as exc:
        logger.exception("Google OAuth callback failed redirect_uri=%s", GOOGLE_REDIRECT_URI)
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {exc}") from exc

    user_email = (userinfo.get("email") or "").strip().lower()
    if ALLOWED_GOOGLE_EMAIL and user_email != ALLOWED_GOOGLE_EMAIL:
        raise HTTPException(status_code=403, detail="This Google account is not allowed")

    session_id, _session = _create_session(userinfo, auth_mode="web")

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("g_oauth_state")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=AUTH_COOKIE_SECURE,
    )
    return response


@app.get("/auth/mobile/config")
def auth_mobile_config():
    return JSONResponse(
        {
            "auth_required": AUTH_REQUIRED,
            "google_sign_in_configured": bool(GOOGLE_SERVER_CLIENT_ID),
            "google_server_client_id": GOOGLE_SERVER_CLIENT_ID,
            "google_android_client_id": GOOGLE_ANDROID_CLIENT_ID,
        }
    )


@app.post("/auth/mobile/google")
def auth_mobile_google(payload: dict):
    if not AUTH_REQUIRED:
        session_id, session = _create_session(
            {"sub": "anonymous", "email": "anonymous@local", "name": "Anonymous"},
            auth_mode="mobile",
        )
        return JSONResponse(
            {
                "ok": True,
                "token": session_id,
                "expires_at": session.get("expires_at"),
                "user": {
                    "sub": session.get("sub"),
                    "email": session.get("email"),
                    "name": session.get("name"),
                    "picture": session.get("picture"),
                },
            }
        )

    if not GOOGLE_SERVER_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Mobile Google auth is not configured")

    id_token = (payload.get("id_token") or "").strip()
    if not id_token:
        raise HTTPException(status_code=400, detail="Missing Google ID token")

    try:
        userinfo = _google_userinfo_from_id_token(id_token)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Mobile Google auth failed")
        raise HTTPException(status_code=400, detail=f"Google token validation failed: {exc}") from exc

    session_id, session = _create_session(userinfo, auth_mode="mobile")
    return JSONResponse(
        {
            "ok": True,
            "token": session_id,
            "expires_at": session.get("expires_at"),
            "user": {
                "sub": session.get("sub"),
                "email": session.get("email"),
                "name": session.get("name"),
                "picture": session.get("picture"),
            },
        }
    )


@app.get("/auth/me")
def auth_me(request: FastAPIRequest):
    session = _session_from_request(request)
    if not session:
        return JSONResponse({"authenticated": False})
    slack_record = _get_slack_user_record(session) or {}
    return JSONResponse(
        {
            "authenticated": True,
            "user": {
                "sub": session.get("sub"),
                "email": session.get("email"),
                "name": session.get("name"),
                "picture": session.get("picture"),
            },
            "auth_mode": session.get("auth_mode", "web"),
            "slack": {
                "connected": bool(slack_record.get("access_token")),
                "user_id": slack_record.get("slack_user_id"),
                "team_name": slack_record.get("team_name"),
            },
        }
    )


@app.post("/auth/logout")
def auth_logout(request: FastAPIRequest):
    session_id = _session_id_from_request(request)
    if session_id:
        sessions.pop(session_id, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/auth/slack/login")
def auth_slack_login(request: FastAPIRequest):
    _require_slack_oauth_config()
    session = _require_auth(request)
    state = secrets.token_urlsafe(24)
    session["slack_oauth_state"] = state
    params = urlencode(
        {
            "client_id": SLACK_CLIENT_ID,
            "redirect_uri": SLACK_REDIRECT_URI,
            "response_type": "code",
            "user_scope": SLACK_USER_SCOPES,
            "state": state,
        }
    )
    return RedirectResponse(url=f"https://slack.com/oauth/v2/authorize?{params}", status_code=302)


@app.get("/auth/slack/callback")
def auth_slack_callback(
    request: FastAPIRequest,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    _require_slack_oauth_config()
    session = _require_auth(request)

    if error:
        raise HTTPException(status_code=400, detail=f"Slack OAuth failed: {error}")

    expected_state = session.get("slack_oauth_state")
    if not code or not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid Slack OAuth state")

    try:
        token_data = _json_post(
            "https://slack.com/api/oauth.v2.access",
            {
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": SLACK_REDIRECT_URI,
            },
        )
        if not token_data.get("ok"):
            raise RuntimeError(token_data.get("error") or "Slack token exchange failed")
        authed_user = token_data.get("authed_user") or {}
        access_token = (authed_user.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("No Slack user access token returned")

        _set_slack_user_record(
            session,
            {
                "access_token": access_token,
                "slack_user_id": authed_user.get("id"),
                "scope": authed_user.get("scope"),
                "team_id": (token_data.get("team") or {}).get("id"),
                "team_name": (token_data.get("team") or {}).get("name"),
                "updated_at": int(time.time()),
            },
        )
    except Exception as exc:
        logger.exception("Slack OAuth callback failed")
        raise HTTPException(status_code=400, detail=f"Slack OAuth exchange failed: {exc}") from exc
    finally:
        session.pop("slack_oauth_state", None)

    return RedirectResponse(url="/", status_code=302)


@app.get("/auth/slack/status")
def auth_slack_status(request: FastAPIRequest):
    session = _require_auth(request)
    record = _get_slack_user_record(session) or {}
    return JSONResponse(
        {
            "connected": bool(record.get("access_token")),
            "user_id": record.get("slack_user_id"),
            "team_name": record.get("team_name"),
            "scope": record.get("scope"),
        }
    )


@app.post("/auth/slack/disconnect")
def auth_slack_disconnect(request: FastAPIRequest):
    session = _require_auth(request)
    _delete_slack_user_record(session)
    session.pop("pending_slack_send", None)
    return JSONResponse({"ok": True})


@app.get("/slack/events")
def slack_events_get():
    return JSONResponse({"ok": True, "message": "Slack endpoint is reachable. Use POST for events."})


@app.post("/slack/events")
async def slack_events(request: FastAPIRequest):
    raw_body = await request.body()
    if not _verify_slack_signature(request, raw_body):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(status_code=400, detail="Missing challenge")
        return JSONResponse({"challenge": challenge})

    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event", {})
    event_type = event.get("type")
    event_subtype = event.get("subtype")

    if event_subtype == "bot_message":
        return JSONResponse({"ok": True})

    is_dm = event_type == "message" and event.get("channel_type") == "im"
    is_mention = event_type == "app_mention"

    if is_dm or is_mention:
        bot_user_id = None
        authorizations = payload.get("authorizations") or []
        if authorizations and isinstance(authorizations, list):
            bot_user_id = (authorizations[0] or {}).get("user_id")

        raw_text = (event.get("text") or "").replace("\n", " ").strip()
        command_text = _strip_bot_mention(raw_text, bot_user_id)
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")

        logger.info(
            "Slack event accepted event_id=%s type=%s user=%s channel=%s text=%s",
            payload.get("event_id"),
            event_type,
            event.get("user"),
            channel,
            command_text[:300],
        )

        if not channel:
            return JSONResponse({"ok": True})

        try:
            target_user_id = _parse_last_message_target_user_id(command_text)
            if not target_user_id:
                _slack_post_message(
                    channel,
                    "Ask me like: `last message from @username` or `last message from <@U12345>`.",
                    thread_ts=thread_ts,
                )
                return JSONResponse({"ok": True})

            message = _get_last_message_from_user(channel, target_user_id)
            if not message:
                _slack_post_message(
                    channel,
                    f"I couldn't find a recent message from <@{target_user_id}> in this channel.",
                    thread_ts=thread_ts,
                )
                return JSONResponse({"ok": True})

            msg_text = (message.get("text") or "").strip()
            permalink = _slack_try_get_permalink(channel, message.get("ts"))

            reply = f"Last message from <@{target_user_id}>:\n>{msg_text}"
            if permalink:
                reply += f"\n{permalink}"
            _slack_post_message(channel, reply, thread_ts=thread_ts)
        except Exception as exc:
            logger.exception("Slack command handling failed")
            _slack_post_message(
                channel,
                f"Error processing request: {exc}",
                thread_ts=thread_ts,
            )
    else:
        logger.info(
            "Slack event ignored event_id=%s type=%s subtype=%s",
            payload.get("event_id"),
            event_type,
            event_subtype,
        )

    return JSONResponse({"ok": True})


@app.post("/transcribe")
@app.post("/transcribe/")
async def transcribe(request: FastAPIRequest, file: UploadFile = File(...)):
    _require_auth(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    suffix = Path(file.filename).suffix or ".webm"
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio")

    transcript = None
    model_errors: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()

        for model in ("gpt-4o-mini-transcribe", "whisper-1"):
            try:
                with open(tmp.name, "rb") as audio_f:
                    transcript = client.audio.transcriptions.create(
                        model=model,
                        file=audio_f,
                        response_format="json",
                    )
                break
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                model_errors.append(f"{model}: {err_msg}")
                logger.exception("Transcription failed with model %s", model)

    text = getattr(transcript, "text", None)
    if text is None and isinstance(transcript, dict):
        text = transcript.get("text")
    if text is None and hasattr(transcript, "model_dump"):
        try:
            text = transcript.model_dump().get("text")
        except Exception:
            text = None
    if text is None:
        if model_errors:
            raise HTTPException(
                status_code=500,
                detail=f"Transcription failed. Attempts: {' | '.join(model_errors)}",
            )
        raise HTTPException(status_code=500, detail="No transcript returned")

    return JSONResponse({"text": text.strip()})


@app.post("/summarize_email")
async def summarize_email(request: FastAPIRequest, payload: dict):
    _require_auth(request)
    conversation = payload.get("conversation")
    to_email = payload.get("to") or DEFAULT_TO_EMAIL
    result = _send_summary_email(conversation, to_email)
    return JSONResponse({"ok": True, "to": result["to"], "subject": result["subject"]})


@app.post("/chat")
async def chat(request: FastAPIRequest, payload: dict):
    session = _require_auth(request)
    message = payload.get("message")
    history = payload.get("history", [])

    if not message or not isinstance(message, str):
        raise HTTPException(status_code=400, detail="Missing user message")
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="History must be a list")

    user_message = message.strip()
    slack_token = _slack_user_token(session)
    connect_hint = "Connect Slack first at /auth/slack/login."
    pending_action = session.get("pending_action")
    if not pending_action and isinstance(session.get("pending_slack_send"), dict):
        pending_action = session.get("pending_slack_send")
        pending_action["type"] = "slack_send_message"
        session["pending_action"] = pending_action
        session.pop("pending_slack_send", None)

    plan = _plan_chat_action(
        user_message=user_message,
        history=history,
        slack_connected=bool(slack_token),
        pending_action=pending_action if isinstance(pending_action, dict) else None,
    )
    planned_action = plan.get("action") or {"type": "none"}
    action_type = (planned_action.get("type") or "none").strip().lower()

    if action_type == "slack_connect":
        if not (SLACK_CLIENT_ID and SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI):
            return JSONResponse(
                {
                    "reply": "Slack OAuth is not configured on this server.",
                    "action": {"type": "slack_connect", "status": "unavailable"},
                }
            )
        return JSONResponse(
            {
                "reply": "Use this link to connect Slack: /auth/slack/login",
                "action": {"type": "slack_connect", "status": "ready"},
            }
        )

    if action_type == "cancel_pending":
        if not isinstance(pending_action, dict):
            return JSONResponse(
                {"reply": "There is no pending Slack or email action to cancel.", "action": {"type": "cancel_pending"}}
            )
        session.pop("pending_action", None)
        session.pop("pending_slack_send", None)
        pending_type = (pending_action.get("type") or "").strip()
        noun = "action"
        if pending_type == "slack_send_message":
            noun = "Slack draft"
        elif pending_type == "email_summary":
            noun = "summary email"
        return JSONResponse(
            {"reply": f"Canceled. I did not send the {noun}.", "action": {"type": "cancel_pending", "status": "canceled"}}
        )

    if action_type == "confirm_pending":
        if not isinstance(pending_action, dict):
            return JSONResponse(
                {"reply": "There is no pending Slack or email action to confirm.", "action": {"type": "confirm_pending"}}
            )
        pending_type = (pending_action.get("type") or "").strip()
        if pending_type == "slack_send_message":
            if not slack_token:
                return JSONResponse(
                    {
                        "reply": f"{connect_hint} Your pending draft was kept.",
                        "action": {"type": "confirm_pending", "status": "blocked"},
                    }
                )
            try:
                channel = pending_action.get("channel")
                text = pending_action.get("text")
                target_label = (pending_action.get("target_user_label") or "").strip()
                if not channel or not text:
                    session.pop("pending_action", None)
                    return JSONResponse(
                        {
                            "reply": "Pending Slack draft was invalid and has been cleared.",
                            "action": {"type": "confirm_pending", "status": "invalid"},
                        }
                    )
                sent = _slack_api_call(
                    "chat.postMessage",
                    {"channel": channel, "text": text},
                    token=slack_token,
                )
                session.pop("pending_action", None)
                sent_ts = (sent.get("ts") or "").strip()
                permalink = _slack_try_get_permalink(channel, sent_ts, slack_token)
                exists = _slack_message_exists(channel, sent_ts, slack_token) if sent_ts else False
                target_display = target_label or "that user"
                reply = f"Sent as you to {target_display}:\n{text}"
                if permalink:
                    reply += f"\n{permalink}"
                if sent_ts:
                    reply += f"\n(channel `{channel}`, ts `{sent_ts}`)"
                if not exists:
                    reply += "\nWarning: Slack accepted the post, but I could not verify it in recent history."
                return JSONResponse(
                    {"reply": reply, "action": {"type": "slack_send_message", "status": "sent"}}
                )
            except Exception as exc:
                logger.exception("Slack send-as-user failed")
                return JSONResponse(
                    {
                        "reply": f"Failed to send Slack message: {exc}",
                        "action": {"type": "slack_send_message", "status": "failed"},
                    }
                )
        if pending_type == "email_summary":
            try:
                result = _send_summary_email(
                    pending_action.get("conversation") or "",
                    pending_action.get("to") or DEFAULT_TO_EMAIL,
                )
                session.pop("pending_action", None)
                return JSONResponse(
                    {
                        "reply": f"I emailed your summary to {result['to']}.",
                        "action": {"type": "email_summary", "status": "sent", "to": result["to"]},
                    }
                )
            except Exception as exc:
                logger.exception("Summary email send failed")
                return JSONResponse(
                    {
                        "reply": f"Email send failed: {exc}",
                        "action": {"type": "email_summary", "status": "failed"},
                    }
                )

    if action_type == "slack_read_inbox":
        if not slack_token:
            return JSONResponse({"reply": connect_hint, "action": {"type": "slack_read_inbox", "status": "blocked"}})
        try:
            auth_data = _slack_api_call("auth.test", {}, token=slack_token)
            self_user_id = auth_data.get("user_id")
            if not self_user_id:
                return JSONResponse(
                    {
                        "reply": "Slack auth test did not return your user id.",
                        "action": {"type": "slack_read_inbox", "status": "failed"},
                    }
                )

            latest = _slack_latest_incoming_dms(slack_token, self_user_id, limit=5)
            if not latest:
                return JSONResponse(
                    {
                        "reply": "I couldn't find any recent incoming Slack DMs.",
                        "action": {"type": "slack_read_inbox", "status": "empty"},
                    }
                )

            name_cache: dict[str, str] = {}
            lines = []
            for item in latest:
                sender_id = item.get("user") or ""
                sender_name = _slack_user_display_name(sender_id, slack_token, name_cache)
                snippet = (item.get("text") or "").replace("\n", " ").strip()
                if len(snippet) > 200:
                    snippet = f"{snippet[:200]}..."
                lines.append(f"- {sender_name}: {snippet}")
            return JSONResponse(
                {
                    "reply": "Latest Slack messages sent directly to you:\n"
                    + "\n".join(lines)
                    + "\n\nTell me what to send and I can draft the reply for confirmation.",
                    "action": {"type": "slack_read_inbox", "status": "ok"},
                }
            )
        except Exception as exc:
            logger.exception("Slack inbox lookup failed")
            return JSONResponse(
                {
                    "reply": f"Slack inbox lookup failed: {exc}",
                    "action": {"type": "slack_read_inbox", "status": "failed"},
                }
            )

    if action_type == "slack_read_user":
        target_name = (planned_action.get("target_name") or "").strip()
        if not target_name:
            return JSONResponse({"reply": "Who should I check in Slack?", "action": {"type": "none"}})
        if not slack_token:
            return JSONResponse({"reply": connect_hint, "action": {"type": "slack_read_user", "status": "blocked"}})
        try:
            target_user_id = _resolve_user_id_from_name(target_name, token=slack_token)
            if not target_user_id:
                return JSONResponse(
                    {
                        "reply": f"I couldn't find Slack user `{target_name}`.",
                        "action": {"type": "slack_read_user", "status": "missing_target"},
                    }
                )
            dm_message, dm_channel = _get_last_dm_message_from_user(target_user_id, token=slack_token)
            if not dm_message or not dm_channel:
                return JSONResponse(
                    {
                        "reply": f"I couldn't find a recent Slack DM message from {target_name}.",
                        "action": {"type": "slack_read_user", "status": "empty"},
                    }
                )
            msg_text = (dm_message.get("text") or "").strip()
            permalink = _slack_try_get_permalink(dm_channel, dm_message.get("ts"), slack_token)
            sender_name = _slack_user_display_name(target_user_id, slack_token, {})
            sender_label = sender_name if sender_name != target_user_id else _clean_requested_name(target_name)
            reply = f"Last Slack message from {sender_label}:\n{msg_text}"
            if permalink:
                reply += f"\n{permalink}"
            return JSONResponse(
                {"reply": reply, "action": {"type": "slack_read_user", "status": "ok", "target_name": sender_label}}
            )
        except Exception as exc:
            logger.exception("Slack lookup from /chat failed")
            return JSONResponse(
                {
                    "reply": f"Slack lookup failed: {exc}",
                    "action": {"type": "slack_read_user", "status": "failed"},
                }
            )

    if action_type == "slack_send_message":
        target_name = (planned_action.get("target_name") or "").strip()
        draft_text = (planned_action.get("text") or "").strip()
        if not target_name or not draft_text:
            return JSONResponse(
                {
                    "reply": "Tell me who to message and what to say, and I will draft it for confirmation.",
                    "action": {"type": "none"},
                }
            )
        if not slack_token:
            return JSONResponse({"reply": connect_hint, "action": {"type": "slack_send_message", "status": "blocked"}})
        try:
            target_user_id = _resolve_user_id_from_name(target_name, token=slack_token)
            if not target_user_id:
                return JSONResponse(
                    {
                        "reply": f"I couldn't find Slack user `{target_name}`.",
                        "action": {"type": "slack_send_message", "status": "missing_target"},
                    }
                )
            resolved_name = _slack_user_display_name(target_user_id, slack_token, {})
            if resolved_name == target_user_id:
                resolved_name = _clean_requested_name(target_name)
            target_label = resolved_name or "that user"
            open_data = _slack_api_call("conversations.open", {"users": target_user_id}, token=slack_token)
            channel = (open_data.get("channel") or {}).get("id")
            if not channel:
                return JSONResponse(
                    {
                        "reply": "I couldn't open a DM channel for that user.",
                        "action": {"type": "slack_send_message", "status": "failed"},
                    }
                )
            session["pending_action"] = {
                "type": "slack_send_message",
                "target_user_id": target_user_id,
                "target_user_label": target_label,
                "channel": channel,
                "text": draft_text,
                "created_at": int(time.time()),
            }
            return JSONResponse(
                {
                    "reply": (
                        f"Draft ready to send as you to {target_label}:\n"
                        f"{draft_text}\n\nSay `send it` to confirm or `cancel`."
                    ),
                    "action": {"type": "slack_send_message", "status": "pending", "target_name": target_label},
                }
            )
        except Exception as exc:
            logger.exception("Slack reply draft failed")
            return JSONResponse(
                {
                    "reply": f"Couldn't prepare Slack reply: {exc}",
                    "action": {"type": "slack_send_message", "status": "failed"},
                }
            )

    if action_type == "email_summary":
        conversation = _conversation_text_from_history(history)
        if not conversation:
            return JSONResponse(
                {
                    "reply": "I need some conversation history before I can email a summary.",
                    "action": {"type": "email_summary", "status": "missing_conversation"},
                }
            )
        to_email = (planned_action.get("to") or "").strip() or DEFAULT_TO_EMAIL
        if not isinstance(to_email, str) or not EMAIL_RE.fullmatch(to_email):
            return JSONResponse(
                {
                    "reply": "What email address should I send the summary to?",
                    "action": {"type": "none"},
                }
            )
        session["pending_action"] = {
            "type": "email_summary",
            "to": to_email,
            "conversation": conversation,
            "created_at": int(time.time()),
        }
        return JSONResponse(
            {
                "reply": f"Summary email is ready for {to_email}. Say `send it` to confirm or `cancel`.",
                "action": {"type": "email_summary", "status": "pending", "to": to_email},
            }
        )

    input_messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for item in history:
        if (
            isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
            and item.get("content").strip()
        ):
            input_messages.append(
                {"role": item["role"], "content": item["content"].strip()}
            )
    input_messages.append({"role": "user", "content": user_message})

    reply = _generate_text(input_messages, model="gpt-4o-mini")
    if not reply:
        raise HTTPException(status_code=500, detail="Empty assistant response")

    return JSONResponse({"reply": reply, "action": {"type": "none"}})


@app.post("/speak")
async def speak(request: FastAPIRequest, payload: dict):
    _require_auth(request)
    text = payload.get("text")
    voice = payload.get("voice", "alloy")
    if not text or not isinstance(text, str):
        raise HTTPException(status_code=400, detail="Missing text")
    if not isinstance(voice, str) or voice not in TTS_VOICES:
        raise HTTPException(status_code=400, detail="Invalid voice")

    input_text = text.strip()
    if not input_text:
        raise HTTPException(status_code=400, detail="Empty text")
    if len(input_text) > 2000:
        input_text = input_text[:2000]

    last_error = None
    for model in ("gpt-4o-mini-tts", "tts-1"):
        try:
            speech = client.audio.speech.create(
                model=model,
                voice=voice,
                input=input_text,
                response_format="mp3",
            )
            audio_bytes = getattr(speech, "content", None)
            if not audio_bytes:
                try:
                    audio_bytes = speech.read()
                except Exception:
                    audio_bytes = None
            if not audio_bytes:
                raise RuntimeError("No audio returned")

            return StreamingResponse(io.BytesIO(audio_bytes), media_type="audio/mpeg")
        except Exception as exc:
            last_error = exc

    raise HTTPException(status_code=500, detail=f"TTS failed: {last_error}")
