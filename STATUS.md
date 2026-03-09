# Project Status

## Session Handoff (Read First)
- Date: 2026-03-08
- State: working
- Current blocker: none.
- Last verified behavior: launch script starts server and prints URL by default (`--open` opens browser), speech defaults are `sage` + `1.3x`, empty transcript no longer shows as 500, and user reports HTTPS is working at the EC2 domain.
- Next step (single action): Implement Slack user OAuth flow so the app can use the signed-in user's token (not bot token) to read personal history and send replies as that user.
- Next command to run: inspect current Slack app scopes/settings and design OAuth callback storage path in backend.
- Expected result: authenticated user token is available server-side for `/chat` Slack intents; app can read that user's own DM/channel history and prepare/send replies with explicit confirmation.
- If fails, do this: verify Slack user scopes are requested, reinstall app, confirm token persistence, and check backend logs for OAuth/token errors.

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
- Google OAuth login is now added (Google sign-in, callback, session cookie, auth status, logout).
- Backend API routes now require authentication by default (configurable via `AUTH_REQUIRED`).
- The app records audio continuously in session mode, transcribes it with OpenAI, gets ChatGPT replies, and can speak replies via OpenAI TTS.
- Recording now auto-submits on silence after speech is detected.
- Natural email-intent phrases now trigger SES email sending flow (not just one exact phrase).
- Email-intent utterances are intercepted client-side and are not sent to `/chat`.
- After a successful send, the assistant confirms in transcript and voice that the email was sent.
- TTS playback speed is user-selectable (`1.0x`, `1.15x`, `1.25x`, `1.3x`, `1.35x`, `1.4x`).
- Default TTS voice is now `sage`; default playback speed is now `1.3x`.
- `/transcribe` now accepts both `/transcribe` and `/transcribe/`.
- Empty transcript responses are handled as a normal no-speech case (UI prompt) instead of backend 500.
- SES is set up in AWS **US West (Oregon)** (`us-west-2`).
- SES is still in **sandbox**; recipients must be verified.
- Verified sender email is configured in `server/.env`.
- Credential source is IAM user static access keys in `server/.env`.

## Files Added
- `server/app.py`: FastAPI backend with `/transcribe`, `/chat`, `/speak`, and `/summarize_email` endpoints
- `server/requirements.txt`
- `server/.env.example`
- `web/index.html`, `web/app.js`, `web/style.css`
- `launch_app.sh`: starts uvicorn, waits for readiness, and can open browser with `--open`
- `README.md`

## Required Configuration
Fill `server/.env` with:
- `OPENAI_API_KEY`
- `PORT=8000` (or your preferred backend port)
- `HOST=0.0.0.0` (for EC2/public binding)
- `SSL_CERTFILE` (optional; set with `SSL_KEYFILE` to enable HTTPS directly in uvicorn)
- `SSL_KEYFILE` (optional; set with `SSL_CERTFILE` to enable HTTPS directly in uvicorn)
- `AWS_REGION=us-west-2` (SES is in us-west-2; ensure `.env` is not `us-east-1`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `SES_FROM_EMAIL` (a verified sender email/domain in SES)
- `DEFAULT_TO_EMAIL` (verified recipient email while SES is in sandbox; any domain is allowed)
- `SLACK_BOT_TOKEN` (for `chat.postMessage`)
- `SLACK_SIGNING_SECRET` (for Slack request verification)
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI`
- `AUTH_REQUIRED=true` (default)
- `AUTH_COOKIE_SECURE=true` on HTTPS hosts (`false` for local HTTP only)
- `ALLOWED_GOOGLE_EMAIL` (optional strict single-user Google login allowlist)

Note: Treat `server/.env` as the source of truth for environment values. If it conflicts with this file, resolve the mismatch by updating `server/.env` first and then reflect it here.

## Open Questions
- Should we store Slack user tokens in a local encrypted file/db for now, or add a managed secret/data store immediately?
- For send-on-behalf actions, what explicit confirmation UX is preferred in web chat (single-turn yes/no vs. separate "Send" action)?

## Completed This Session
- Added `PORT` support via `server/.env`.
- Updated `launch_app.sh` to source `server/.env` so `PORT`/`HOST` are applied automatically.
- Updated `launch_app.sh` so browser auto-open only happens when `--open` is passed.
- Added optional TLS startup in `launch_app.sh` via `SSL_CERTFILE` + `SSL_KEYFILE`.
- Pinned `httpx<0.28` in `server/requirements.txt` to fix OpenAI client startup (`proxies` argument mismatch).
- Added Google OAuth endpoints in backend:
  - `GET /auth/google/login`
  - `GET /auth/google/callback`
  - `GET /auth/me`
  - `POST /auth/logout`
- Added in-memory session management with secure/httponly cookie.
- Added auth guard to `/transcribe`, `/chat`, `/speak`, and `/summarize_email`.
- Added frontend sign-in/sign-out controls and blocked recording until authenticated.
- Added optional strict Google single-user allowlist via `ALLOWED_GOOGLE_EMAIL`.
- Updated `server/.env.example` and `README.md` with Google OAuth + cookie config.
- Added root launcher script `./launch_app.sh` to start server; pass `--open` to open browser automatically.
- Updated frontend defaults to voice `sage` and speed `1.3x`.
- Added static asset cache-busting query strings in `web/index.html` to avoid stale JS/CSS.
- Added `/transcribe/` route alias alongside `/transcribe`.
- Improved transcription behavior:
  - model fallback from `gpt-4o-mini-transcribe` to `whisper-1`
  - better per-model exception logging
  - empty transcript no longer returns 500
- Updated frontend to show `No speech detected. Try again.` for empty transcripts.
- Added Slack credentials to `server/.env`:
  - `SLACK_BOT_TOKEN`
  - `SLACK_SIGNING_SECRET`
- Confirmed local-only setup needs a tunnel URL (or Socket Mode) before Slack Events can reach the app.
- User installed Certbot nginx plugin on EC2 and reported HTTPS works for the main domain.
- Clarified deployment model: Slack Events require backend (FastAPI/uvicorn) on a public host (EC2 or tunnel), while frontend is browser-loaded static assets.
- Implemented Slack integration phase 1 in backend:
  - `GET /slack/events` reachability response
  - `POST /slack/events` with Slack signature verification (`X-Slack-Signature`, timestamp skew check)
  - URL verification challenge response (`type=url_verification`)
  - Accept DM/app_mention events and log payload metadata
  - Ignore bot_message subtype and non-target event types
- Implemented Slack integration phase 2 (initial command flow):
  - DM/app mention commands now trigger `chat.postMessage` replies.
  - Added command: `last message from <@user>` (or username/display name).
  - Bot reads channel history via `conversations.history`, finds latest matching user message, and replies with quote + permalink.
- Added Slack lookup intent in web chat UI (`POST /chat` path):
  - Prompts like `last message from Alex` / `last a slack message from Alex` are intercepted.
  - Backend resolves Slack user, opens DM conversation, fetches latest message from that person, and returns it in chat reply.
- Confirmed product direction: support reading user's personal Slack history and replying as the user (requires user OAuth tokens and explicit send confirmation flow).

## Next Steps
1. Implement Slack user OAuth install/login flow and callback handling in backend.
2. Store per-user Slack tokens securely and map them to authenticated web sessions.
3. Update `/chat` Slack lookup intent to use the signed-in user's token (personal history access).
4. Add "send as me" action with explicit confirmation before sending.
5. End-to-end test user-token flow (read + draft + confirm send) and verify permissions/scopes.
6. Keep existing email-intent handling precedence so command phrases do not get forwarded to `/chat`.
7. Add voice command phrases to end chat session (for example: “end chat”, “stop chat”, “we’re done”).
8. Add optional voice command phrases to start a new chat/reset conversation context (for example: “new chat”, “start over”).
9. Optional hygiene: add `server/.env` to `.gitignore` to avoid accidental secret commits.

## Testing URL Reminder
- Prefer the backend-served URL `http://localhost:8000` for local testing.
- Do not open `web/index.html` directly from the filesystem unless explicitly debugging static files.

## Reminder
- If you see SES `AccessDenied` for `ses:SendEmail`, it is an IAM permission issue, not an OpenAI issue.
- ChatGPT Plus does not include OpenAI API credits; API billing must be configured separately.

## OpenAI Billing Runbook
1. Sign in to the OpenAI API platform (not ChatGPT).
2. Open Billing, add a payment method, and buy credits.
3. Wait a couple minutes for the balance to update, then retry the app.
