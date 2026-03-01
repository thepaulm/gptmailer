# Voice Summary Mailer

Minimal web app that records audio, transcribes it with OpenAI, and emails a summary via AWS SES when you say “email me a summary”.

## Setup

1. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
```

2. Configure env:

```bash
cp server/.env.example server/.env
```

Fill in:
- `OPENAI_API_KEY`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `SES_FROM_EMAIL`
- `DEFAULT_TO_EMAIL`

3. Run server:

```bash
uvicorn server.app:app --reload --port 8000
```

Open `http://localhost:8000` (preferred for normal testing).
Do not open `web/index.html` directly from the filesystem unless you are explicitly debugging static assets.

## Notes
- Browser mic requires HTTPS on mobile. `http://localhost` works for local testing.
- Click `Start Recording` once to begin a continuous chat session; use `End Chat` to stop.
- The app auto-submits a turn after a brief pause in speech.
- Say “email me a summary” to trigger the email. To send to another recipient, say the full email address in the same utterance.
- If SES send fails with `AccessDenied`, update IAM permissions for the AWS user to allow `ses:SendEmail`/`ses:SendRawEmail` in `us-west-2`.
