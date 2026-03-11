# Project Status

## Session Handoff (Read First)
- Date: 2026-03-11
- State: working
- Current blocker: none.
- Last verified behavior: `/chat` now routes normal assistant replies, Slack intents, and email-summary requests through a structured server-side action plan; the web client no longer intercepts email-summary phrases.
- Next step (single action): Test the new structured `/chat` action architecture end to end on the target deployment.
- Next command to run: start app, have a short conversation, ask `email me a summary`, then `send it`; then ask `latest slack messages to me`, then `send slack message to <name>: ...`, then `send it`.
- Expected result: the model returns a structured action plan, Python stages the email/Slack action, and explicit confirmation triggers the actual send.
- If fails, do this: inspect backend logs for structured-planning JSON errors, confirm OpenAI chat-completions JSON mode works with the installed SDK/model, and verify Slack OAuth/scopes/token persistence.

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
- Google OAuth login is now added (Google sign-in, callback, session cookie, auth status, logout).
- Backend API routes now require authentication by default (configurable via `AUTH_REQUIRED`).
- The app records audio continuously in session mode, transcribes it with OpenAI, gets ChatGPT replies, and can speak replies via OpenAI TTS.
- Recording now auto-submits on silence after speech is detected.
- Natural email-intent phrases now route through `/chat` instead of client-side interception.
- `/chat` now uses a structured action-planning step so Python can stage and execute Slack/email actions.
- Email and Slack side effects now require explicit confirmation from a pending server-side action.
- After a successful send, the assistant confirms in transcript and voice that the action completed.
- TTS playback speed is user-selectable (`1.0x`, `1.15x`, `1.25x`, `1.3x`, `1.35x`, `1.4x`).
- Default TTS voice is now `sage`; default playback speed is now `1.3x`.
- `/transcribe` now accepts both `/transcribe` and `/transcribe/`.
- Empty transcript responses are handled as a normal no-speech case (UI prompt) instead of backend 500.
- SES is set up in AWS **US West (Oregon)** (`us-west-2`).
- SES is still in **sandbox**; recipients must be verified.
- Verified sender email is configured in `server/.env`.
- Credential source is IAM user static access keys in `server/.env`.
- Slack user OAuth (per-user token flow) is now implemented for read/send-as-user behavior in `/chat`.

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
- `SLACK_CLIENT_ID`
- `SLACK_CLIENT_SECRET`
- `SLACK_REDIRECT_URI`
- `SLACK_USER_SCOPES` (user-token scopes for read/send flows)
- `SLACK_TOKEN_STORE_PATH` (local token persistence path)
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
- Loosened Slack name resolution in `server/app.py`:
  - accepts compact-name matches like `Paul M` -> `paulm`
  - accepts close vowel variants like `John May` -> `John Mey`
  - keeps fuzzy fallback conservative to reduce wrong-user matches
- Reworked `/chat` into the single server-side action router for normal replies, Slack actions, and email-summary requests.
- Added a structured action-planning prompt in `server/app.py` so the model returns JSON describing the requested action.
- Added server-side staging and confirmation for summary emails in `/chat`.
- Migrated Slack send staging from `pending_slack_send` to a general `pending_action` flow, with legacy-session migration.
- Removed client-side email phrase interception from `web/app.js`; all user utterances now go through `/chat`.
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
- Implemented Slack user OAuth endpoints:
  - `GET /auth/slack/login`
  - `GET /auth/slack/callback`
  - `GET /auth/slack/status`
  - `POST /auth/slack/disconnect`
- Added per-user Slack token persistence in local JSON store (`SLACK_TOKEN_STORE_PATH`).
- Updated web auth header with Slack connect/disconnect controls and connection status.
- Updated `/chat` Slack intents to use signed-in user token:
  - Read latest incoming DMs with prompts like `latest slack messages to me`.
  - Prepare send-as-user draft with `reply to @name: ...`.
  - Require explicit confirmation (`send it`) before sending.
  - Support `cancel` to discard pending draft.
- Expanded Slack send draft command parsing:
  - `send slack message to <name>: ...`
  - `send message to <name>: ...`
  - names with spaces are supported.
- Fixed Slack send confirmation parsing to accept natural punctuation (`send it.`, `send it!`, `cancel.`).
- Fixed false email-summary trigger on `send it` so Slack confirmation reaches `/chat`.
- Made Slack permalink generation best-effort so read/send actions do not fail when `chat.getPermalink` returns `invalid_arguments`.
- Added post-send verification by reading message `ts` from channel history and reporting channel/ts in confirmation text.
- Updated Slack response formatting to prefer friendly names and avoid raw Slack IDs in user-facing messages.

## Next Steps
1. Test the new structured `/chat` action architecture end to end for both email summary and Slack flows.
2. Verify Slack app user scopes/redirect URI and reinstall the app if scopes changed.
3. End-to-end test user-token flow (connect + read + draft + confirm send) on local and EC2.
4. Decide whether to encrypt local token store immediately or move to managed secret/data store.
5. Add a dedicated UI confirmation button for Slack sends (optional) to reduce reliance on phrase matching.
6. Add voice command phrases to end chat session (for example: “end chat”, “stop chat”, “we’re done”).
7. Add optional voice command phrases to start a new chat/reset conversation context (for example: “new chat”, “start over”).
8. Optional hygiene: add `server/.env` and token store path to `.gitignore` to avoid accidental secret commits.

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
