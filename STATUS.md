# Project Status

## Session Handoff (Read First)
- Date: 2026-03-01
- State: blocked
- Current blocker: OpenAI API `insufficient_quota` (HTTP 429) during transcription.
- Next step (single action): Enable OpenAI API billing/add credits, then retry transcription.
- Next command to run: `cd server && source ~/py3/bin/activate && uvicorn app:app --reload`
- Expected result: `/transcribe` succeeds (HTTP 200) without OpenAI quota errors.
- If fails, do this: Confirm API key belongs to the billed OpenAI project, confirm positive API credit balance, wait 2-5 minutes, then retry.

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
- The app records audio, transcribes it with OpenAI, and triggers SES email sending on the voice phrase.
- SES is set up in AWS **US West (Oregon)** (`us-west-2`).
- SES is still in **sandbox**; recipients must be verified.
- Verified sender email is `pmikesell@pgntrain.com`.
- Credential source is IAM user static access keys in `server/.env`.

## Files Added
- `server/app.py`: FastAPI backend with `/transcribe` and `/summarize_email` endpoints
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
- Updated sender to `pmikesell@pgntrain.com` and documented IAM user key usage.
- Corrected SES note: sandbox requires verified recipients, but recipient domain can differ from sender domain.

## Next Steps
1. Complete the single action in `Session Handoff (Read First)`.
2. Run server and test voice capture + email end to end.
3. Confirm both sender and recipient are verified in SES sandbox before testing sends.
4. If testing on Android over the network, add HTTPS (tunnel or deploy).
5. Move SES out of sandbox when ready for unverified recipient sends.

## Reminder
- If you see OpenAI `insufficient_quota` (HTTP 429), enable API billing and add credits in the OpenAI API platform. ChatGPT Plus does not include API usage.

## OpenAI Billing Runbook
1. Sign in to the OpenAI API platform (not ChatGPT).
2. Open Billing, add a payment method, and buy credits.
3. Wait a couple minutes for the balance to update, then retry the app.
