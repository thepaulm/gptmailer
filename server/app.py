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
from urllib.parse import urlencode
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


def _session_from_request(request: FastAPIRequest) -> dict | None:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
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
    normalized = (name or "").strip().lower().lstrip("@").rstrip(".,!?")
    if not normalized:
        return None

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
            candidates = {
                (member.get("name") or "").lower(),
                (profile.get("display_name") or "").lower(),
                (profile.get("real_name") or "").lower(),
            }
            if normalized in candidates:
                return member.get("id")
            if any(c.startswith(normalized) for c in candidates if c):
                return member.get("id")
            if any(normalized in c.split() for c in candidates if c):
                return member.get("id")
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return None


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


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/auth/google/login")
def auth_google_login():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")

    state = secrets.token_urlsafe(24)
    params = urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
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
        logger.exception("Google OAuth callback failed")
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {exc}") from exc

    user_email = (userinfo.get("email") or "").strip().lower()
    if ALLOWED_GOOGLE_EMAIL and user_email != ALLOWED_GOOGLE_EMAIL:
        raise HTTPException(status_code=403, detail="This Google account is not allowed")

    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
        "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
    }

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
            "slack": {
                "connected": bool(slack_record.get("access_token")),
                "user_id": slack_record.get("slack_user_id"),
                "team_name": slack_record.get("team_name"),
            },
        }
    )


@app.post("/auth/logout")
def auth_logout(request: FastAPIRequest):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
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

    if not conversation or not isinstance(conversation, str):
        raise HTTPException(status_code=400, detail="Missing conversation text")

    if not EMAIL_RE.fullmatch(to_email):
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

    return JSONResponse({"ok": True, "to": to_email, "subject": subject})


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
    lowered = user_message.lower()
    slack_token = _slack_user_token(session)
    connect_hint = "Connect Slack first at /auth/slack/login."

    if lowered in {"connect slack", "link slack", "authorize slack"}:
        if not (SLACK_CLIENT_ID and SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI):
            return JSONResponse({"reply": "Slack OAuth is not configured on this server."})
        return JSONResponse({"reply": "Use this link to connect Slack: /auth/slack/login"})

    pending_send = session.get("pending_slack_send")
    if pending_send and isinstance(pending_send, dict):
        if SLACK_SEND_CANCEL_RE.match(user_message):
            session.pop("pending_slack_send", None)
            return JSONResponse({"reply": "Canceled. I did not send the Slack message."})
        if SLACK_SEND_CONFIRM_RE.match(user_message):
            if not slack_token:
                return JSONResponse({"reply": f"{connect_hint} Your pending draft was kept."})
            try:
                channel = pending_send.get("channel")
                text = pending_send.get("text")
                target_user = pending_send.get("target_user_id")
                target_label = (pending_send.get("target_user_label") or "").strip()
                if not channel or not text:
                    session.pop("pending_slack_send", None)
                    return JSONResponse({"reply": "Pending Slack draft was invalid and has been cleared."})
                sent = _slack_api_call(
                    "chat.postMessage",
                    {"channel": channel, "text": text},
                    token=slack_token,
                )
                session.pop("pending_slack_send", None)
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
                return JSONResponse({"reply": reply})
            except Exception as exc:
                logger.exception("Slack send-as-user failed")
                return JSONResponse({"reply": f"Failed to send Slack message: {exc}"})

    target_match = LAST_MESSAGE_CMD_RE.search(user_message)
    requested_target_raw = ""
    if target_match:
        requested_target_raw = (target_match.group(1) or "").strip()
    direct_command_user_id = _parse_last_message_target_user_id(user_message, token=slack_token)
    if direct_command_user_id:
        if not slack_token:
            return JSONResponse({"reply": connect_hint})
        try:
            dm_message, dm_channel = _get_last_dm_message_from_user(
                direct_command_user_id, token=slack_token
            )
            if not dm_message or not dm_channel:
                return JSONResponse(
                    {
                        "reply": f"I couldn't find a recent Slack DM message from <@{direct_command_user_id}>."
                    }
                )

            msg_text = (dm_message.get("text") or "").strip()
            permalink = _slack_try_get_permalink(dm_channel, dm_message.get("ts"), slack_token)

            sender_name = _slack_user_display_name(direct_command_user_id, slack_token, {})
            if sender_name == direct_command_user_id and requested_target_raw:
                fallback_name = _clean_requested_name(requested_target_raw)
                if not fallback_name.startswith("<@"):
                    sender_name = fallback_name
            sender_label = sender_name
            reply = f"Last Slack message from {sender_label}:\n{msg_text}"
            if permalink:
                reply += f"\n{permalink}"
            return JSONResponse({"reply": reply})
        except Exception as exc:
            logger.exception("Slack lookup from /chat failed")
            return JSONResponse({"reply": f"Slack lookup failed: {exc}"})

    if SLACK_INBOX_CMD_RE.search(user_message):
        if not slack_token:
            return JSONResponse({"reply": connect_hint})
        try:
            auth_data = _slack_api_call("auth.test", {}, token=slack_token)
            self_user_id = auth_data.get("user_id")
            if not self_user_id:
                return JSONResponse({"reply": "Slack auth test did not return your user id."})

            latest = _slack_latest_incoming_dms(slack_token, self_user_id, limit=5)
            if not latest:
                return JSONResponse({"reply": "I couldn't find any recent incoming Slack DMs."})

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
                    + "\n\nTell me what to send with: `reply to @name: your message`.",
                }
            )
        except Exception as exc:
            logger.exception("Slack inbox lookup failed")
            return JSONResponse({"reply": f"Slack inbox lookup failed: {exc}"})

    reply_match = SLACK_REPLY_CMD_RE.match(user_message)
    if reply_match:
        if not slack_token:
            return JSONResponse({"reply": connect_hint})
        target_user_id = reply_match.group(2)
        target_name = (reply_match.group(1) or "").strip()
        draft_text = (reply_match.group(3) or "").strip()
        if not draft_text:
            return JSONResponse({"reply": "Please include a reply message after the colon."})
        try:
            if not target_user_id:
                target_user_id = _resolve_user_id_from_name(target_name, token=slack_token)
            if not target_user_id:
                return JSONResponse({"reply": f"I couldn't find Slack user `{target_name}`."})
            resolved_name = _slack_user_display_name(target_user_id, slack_token, {})
            if resolved_name == target_user_id:
                resolved_name = _clean_requested_name(target_name)
            target_label = resolved_name or "that user"
            open_data = _slack_api_call("conversations.open", {"users": target_user_id}, token=slack_token)
            channel = (open_data.get("channel") or {}).get("id")
            if not channel:
                return JSONResponse({"reply": "I couldn't open a DM channel for that user."})
            session["pending_slack_send"] = {
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
                    )
                }
            )
        except Exception as exc:
            logger.exception("Slack reply draft failed")
            return JSONResponse({"reply": f"Couldn't prepare Slack reply: {exc}"})

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

    return JSONResponse({"reply": reply})


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
