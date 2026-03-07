# Project Status

## Session Handoff (Read First)
- Date: 2026-03-07
- State: working
- Current blocker: none.
- Last verified behavior: launch script opens app, speech defaults are `sage` + `1.3x`, and empty transcript no longer shows as 500.
- Next step (single action): Set up TLS certificate on EC2 domain so the external HTTPS URL can be used as the Slack Events endpoint.
- Next command to run: `sudo certbot --nginx -d <your-domain> [-d www.<your-domain>]`
- Expected result: valid HTTPS is enabled on EC2 domain; then configure Slack Event Subscriptions to `https://<your-domain>/slack/events`.
- If fails, do this: verify DNS A record points to EC2 static IP, confirm security group allows ports 80/443, check Nginx server block for domain, and rerun certbot with logs.

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
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
- Verified sender email is `pmikesell@pgntrain.com`.
- Credential source is IAM user static access keys in `server/.env`.

## Files Added
- `server/app.py`: FastAPI backend with `/transcribe`, `/chat`, `/speak`, and `/summarize_email` endpoints
- `server/requirements.txt`
- `server/.env.example`
- `web/index.html`, `web/app.js`, `web/style.css`
- `launch_app.sh`: starts uvicorn, waits for readiness, and opens browser
- `README.md`

## Required Configuration
Fill `server/.env` with:
- `OPENAI_API_KEY`
- `AWS_REGION=us-west-2` (SES is in us-west-2; ensure `.env` is not `us-east-1`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `SES_FROM_EMAIL` (verified sender email at `pgntrain.com`)
- `DEFAULT_TO_EMAIL` (verified recipient email while SES is in sandbox; any domain is allowed)
- `SLACK_BOT_TOKEN` (for `chat.postMessage`)
- `SLACK_SIGNING_SECRET` (for Slack request verification)

Note: Treat `server/.env` as the source of truth for environment values. If it conflicts with this file, resolve the mismatch by updating `server/.env` first and then reflect it here.

## Open Questions
- None currently.

## Completed This Session
- Added root launcher script `./launch_app.sh` to start server and open browser automatically.
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

## Next Steps
1. Set up TLS cert on EC2/domain (Let’s Encrypt via certbot) so external HTTPS URL is ready.
2. Configure Slack Event Subscriptions Request URL to `https://<your-domain>/slack/events`.
3. Implement Slack integration phase 1 in backend: verify Slack signature, handle URL verification challenge, accept DM + mention events, and log payloads.
4. Implement Slack integration phase 2: generate reply text and send responses with `chat.postMessage`.
5. End-to-end test in Slack (DM and channel mention) and verify no duplicate responses.
6. Add voice command phrases to end chat session (for example: “end chat”, “stop chat”, “we’re done”).
7. Add optional voice command phrases to start a new chat/reset conversation context (for example: “new chat”, “start over”).
8. Keep existing email-intent handling precedence so command phrases do not get forwarded to `/chat`.
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
