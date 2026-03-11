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
- `PORT` (optional app port, default `8000`)
- `HOST` (optional bind host, default `127.0.0.1`; use `0.0.0.0` on EC2)
- `SSL_CERTFILE` (optional; enable HTTPS in uvicorn when set with `SSL_KEYFILE`)
- `SSL_KEYFILE` (optional; enable HTTPS in uvicorn when set with `SSL_CERTFILE`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `SES_FROM_EMAIL`
- `DEFAULT_TO_EMAIL`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (example local: `http://localhost:8000/auth/google/callback`)
- `AUTH_REQUIRED` (`true` to require login before API usage)
- `AUTH_COOKIE_SECURE` (`false` for local HTTP, `true` for HTTPS/EC2)
- `ALLOWED_GOOGLE_EMAIL` (optional; if set, only this exact Google email can sign in)
- `SLACK_CLIENT_ID`
- `SLACK_CLIENT_SECRET`
- `SLACK_REDIRECT_URI` (example local: `http://localhost:8000/auth/slack/callback`)
- `SLACK_USER_SCOPES` (default includes DM/history/send scopes for user token flow)
- `SLACK_TOKEN_STORE_PATH` (local JSON store for per-user Slack tokens)
- `SLACK_BOT_TOKEN` (optional; only needed for `/slack/events` bot endpoint)
- `SLACK_SIGNING_SECRET` (optional; only needed for `/slack/events` bot endpoint)

3. Run server:

```bash
uvicorn server.app:app --reload --port 8000
```

Open `http://localhost:8000` (preferred for normal testing).
Do not open `web/index.html` directly from the filesystem unless you are explicitly debugging static assets.

Alternative one-command launcher:

```bash
./launch_app.sh
```

Use `./launch_app.sh --open` if you want it to launch your browser automatically.
`launch_app.sh` loads `server/.env` first, so `PORT`/`HOST` set there are used automatically.
If both `SSL_CERTFILE` and `SSL_KEYFILE` are set, it starts uvicorn with HTTPS.

## Notes
- Google OAuth routes:
  - `GET /auth/google/login`
  - `GET /auth/google/callback`
  - `GET /auth/me`
  - `POST /auth/logout`
- Slack user OAuth routes:
  - `GET /auth/slack/login`
  - `GET /auth/slack/callback`
  - `GET /auth/slack/status`
  - `POST /auth/slack/disconnect`
- Browser mic requires HTTPS on mobile. `http://localhost` works for local testing.
- Click `Start Recording` once to begin a continuous chat session; use `End Chat` to stop.
- The app auto-submits a turn after a brief pause in speech.
- `/chat` now acts as the single action router for normal replies, Slack actions, and email-summary requests.
- The frontend no longer intercepts email-summary phrases; all user messages go to `/chat`, where the model returns a structured action plan that Python validates and executes.
- Email summary requests support natural phrasing (for example: `email this`, `send this by email`, `mail me the recap`, `send me the notes`).
- To send to another recipient, say the full email address in the same utterance.
- Side-effecting actions are staged first and require explicit confirmation before Python sends Slack messages or summary emails.
- Next validation step: test this new `/chat` action architecture end to end for both staged email summary and staged Slack send.
- After a successful send, the app posts and can speak a confirmation message (`I emailed your summary to ...`).
- Reply speech speed is adjustable from the UI (`1.0x`, `1.15x`, `1.25x`, `1.3x`, `1.35x`, `1.4x`).
- Default reply voice is `sage` and default speech speed is `1.3x`.
- If transcription returns no recognized speech, the UI now shows `No speech detected. Try again.` instead of a server error.
- If SES send fails with `AccessDenied`, update IAM permissions for the AWS user to allow `ses:SendEmail`/`ses:SendRawEmail` in `us-west-2`.

## Slack User Flow
- Sign in with Google, then click `Connect Slack`.
- Ask: `latest slack messages to me` (or `read my slack`) to fetch recent incoming DMs.
- Fetch a specific user’s latest DM with: `last slack message from <name>`.
- Draft a reply with either:
  - `reply to <name>: your message`
  - `send slack message to <name>: your message`
  - `send message to <name>: your message`
- Confirm with `send it` (also accepts `send it.` / `send it!`) or cancel with `cancel`.
- Messages are sent with your Slack user token, not a bot token.
- User-facing responses prefer friendly names (not raw Slack user IDs).
- Slack routing is model-directed through `/chat`; Python still resolves users, stages drafts, and enforces confirmation before sending.
- Permalink lookup is best-effort; message reads/sends continue even if Slack permalink generation fails.

## Deployment Note (Slack Events)
- For Slack Event Subscriptions, the backend endpoint must be publicly reachable over HTTPS.
- In this project, backend means FastAPI served by `uvicorn` (`server/app.py` endpoints, including `/slack/events`).
- Frontend means the static files in `web/` (`index.html`, `app.js`, `style.css`) that run in the browser.
- Running only on a desktop `localhost` is not enough for Slack Events unless you use a tunnel URL (for example ngrok/Cloudflare Tunnel).

## Deployment Note (Google OAuth on EC2)
- In Google Cloud OAuth credentials, add your EC2 callback URL exactly:
  - `https://<your-domain>/auth/google/callback`
- Set `GOOGLE_REDIRECT_URI` in `server/.env` to that same URL.
- Keep `AUTH_COOKIE_SECURE=true` in production HTTPS environments.
