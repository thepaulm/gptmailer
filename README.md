# Voice Summary Mailer

Minimal web app that records audio, transcribes it with OpenAI, and emails a summary via AWS SES when you ask it to email the conversation summary.

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

Alternative one-command launcher (starts server + opens browser):

```bash
./launch_app.sh
```

## Notes
- Browser mic requires HTTPS on mobile. `http://localhost` works for local testing.
- Click `Start Recording` once to begin a continuous chat session; use `End Chat` to stop.
- The app auto-submits a turn after a brief pause in speech.
- Email summary trigger supports natural phrasing (for example: `email this`, `send this by email`, `mail me the recap`, `send me the notes`).
- To send to another recipient, say the full email address in the same utterance.
- After a successful send, the app posts and can speak a confirmation message (`I emailed your summary to ...`).
- Reply speech speed is adjustable from the UI (`1.0x`, `1.15x`, `1.25x`, `1.3x`, `1.35x`, `1.4x`).
- Default reply voice is `sage` and default speech speed is `1.3x`.
- If transcription returns no recognized speech, the UI now shows `No speech detected. Try again.` instead of a server error.
- If SES send fails with `AccessDenied`, update IAM permissions for the AWS user to allow `ses:SendEmail`/`ses:SendRawEmail` in `us-west-2`.

## Next Milestone
- Slack integration MVP:
  - Read incoming Slack messages (start with mentions/DMs)
  - Post bot responses back to Slack

## Deployment Note (Slack Events)
- For Slack Event Subscriptions, the backend endpoint must be publicly reachable over HTTPS.
- In this project, backend means FastAPI served by `uvicorn` (`server/app.py` endpoints, including `/slack/events`).
- Frontend means the static files in `web/` (`index.html`, `app.js`, `style.css`) that run in the browser.
- Running only on a desktop `localhost` is not enough for Slack Events unless you use a tunnel URL (for example ngrok/Cloudflare Tunnel).
