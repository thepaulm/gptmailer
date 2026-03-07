import os
import re
import tempfile
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")
if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and SES_FROM_EMAIL and DEFAULT_TO_EMAIL):
    raise RuntimeError("Missing AWS/SES environment variables")

client = OpenAI()
logger = logging.getLogger(__name__)

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


def _extract_output_text(resp) -> str:
    if hasattr(resp, "output_text") and resp.output_text:
        return resp.output_text
    try:
        return resp.output[0].content[0].text  # type: ignore[index]
    except Exception:
        return str(resp)


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


@app.post("/transcribe")
@app.post("/transcribe/")
async def transcribe(file: UploadFile = File(...)):
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
async def summarize_email(payload: dict):
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
async def chat(payload: dict):
    message = payload.get("message")
    history = payload.get("history", [])

    if not message or not isinstance(message, str):
        raise HTTPException(status_code=400, detail="Missing user message")
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="History must be a list")

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
    input_messages.append({"role": "user", "content": message.strip()})

    reply = _generate_text(input_messages, model="gpt-4o-mini")
    if not reply:
        raise HTTPException(status_code=500, detail="Empty assistant response")

    return JSONResponse({"reply": reply})


@app.post("/speak")
async def speak(payload: dict):
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
