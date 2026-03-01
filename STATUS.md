# Project Status

## Session Handoff (Read First)
- Date: 2026-03-01
- State: blocked
- Current blocker: AWS SES `AccessDenied` on `ses:SendEmail` for IAM user `gptmailer`.
- Last observed error: `User arn:aws:iam::183631347137:user/gptmailer is not authorized to perform ses:SendEmail`.
- Next step (single action): Add/attach IAM policy granting `ses:SendEmail` + `ses:SendRawEmail` for SES identities in `us-west-2`, then retry email trigger.
- Next command to run: `cd server && source ~/py3/bin/activate && uvicorn app:app --reload`
- Expected result: backend starts and app is reachable at `http://localhost:8000`; saying “email me a summary” succeeds end-to-end (transcribe + chat + TTS + SES send).
- If fails, do this: check CloudWatch/console error; for SES `AccessDenied`, re-check IAM policy scope and that credentials in `server/.env` belong to the updated IAM user.

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
- The app records audio continuously in session mode, transcribes it with OpenAI, gets ChatGPT replies, and can speak replies via OpenAI TTS.
- Recording now auto-submits on silence after speech is detected.
- Saying “email me a summary” triggers SES email sending flow.
- SES is set up in AWS **US West (Oregon)** (`us-west-2`).
- SES is still in **sandbox**; recipients must be verified.
- Verified sender email is `pmikesell@pgntrain.com`.
- Credential source is IAM user static access keys in `server/.env`.

## Files Added
- `server/app.py`: FastAPI backend with `/transcribe`, `/chat`, `/speak`, and `/summarize_email` endpoints
- `server/requirements.txt`
- `server/.env.example`
- `web/index.html`, `web/app.js`, `web/style.css`
- `README.md`

## Required Configuration
Fill `server/.env` with:
- `OPENAI_API_KEY`
- `AWS_REGION=us-west-2` (SES is in us-west-2; ensure `.env` is not `us-east-1`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `SES_FROM_EMAIL` (verified sender email at `pgntrain.com`)
- `DEFAULT_TO_EMAIL` (verified recipient email while SES is in sandbox; any domain is allowed)

Note: Treat `server/.env` as the source of truth for environment values. If it conflicts with this file, resolve the mismatch by updating `server/.env` first and then reflect it here.

## Open Questions
- None currently.

## Completed This Session
- Confirmed `server/.env` has required keys and `AWS_REGION=us-west-2`.
- Added backend chat support and OpenAI SDK compatibility fallback for older clients (`responses` vs `chat.completions`).
- Added OpenAI TTS endpoint and voice playback in web app.
- Added voice selector with full current API voice list (`alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`).
- Added continuous session mode: one click starts chat loop; silence auto-submits turns.
- Fixed transcription parsing for SDK object responses (`Transcription` without `.get`).
- Identified current blocker: SES IAM `AccessDenied` on send.

## Next Steps
1. Attach IAM policy to `gptmailer` allowing `ses:SendEmail` and `ses:SendRawEmail` on `arn:aws:ses:us-west-2:183631347137:identity/*`.
2. Restart server, open `http://localhost:8000`, and test “email me a summary.”
3. Confirm both `SES_FROM_EMAIL` and recipient are verified in SES sandbox (`us-west-2`).
4. After send succeeds, optionally move SES out of sandbox for unverified recipients.
5. Optional hygiene: add `server/.env` to `.gitignore` to avoid accidental secret commits.

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
