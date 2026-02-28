import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

SUMMARY_SYSTEM_PROMPT = (
    "You are summarizing a conversation. Return 5-10 concise bullet points. "
    "Capture decisions, requests, and action items. Avoid fluff."
)


def _extract_output_text(resp) -> str:
    if hasattr(resp, "output_text") and resp.output_text:
        return resp.output_text
    try:
        return resp.output[0].content[0].text  # type: ignore[index]
    except Exception:
        return str(resp)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    suffix = Path(file.filename).suffix or ".webm"
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio")

    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        with open(tmp.name, "rb") as audio_f:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_f,
                response_format="json",
            )

    text = getattr(transcript, "text", None) or transcript.get("text")
    if not text:
        raise HTTPException(status_code=500, detail="No transcript returned")

    return JSONResponse({"text": text})


@app.post("/summarize_email")
async def summarize_email(payload: dict):
    conversation = payload.get("conversation")
    to_email = payload.get("to") or DEFAULT_TO_EMAIL

    if not conversation or not isinstance(conversation, str):
        raise HTTPException(status_code=400, detail="Missing conversation text")

    if not EMAIL_RE.fullmatch(to_email):
        raise HTTPException(status_code=400, detail="Invalid recipient email")

    response = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": conversation},
        ],
    )
    summary = _extract_output_text(response).strip()
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
