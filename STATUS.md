# Project Status

## Goal
Build a fully voice-enabled web app that transcribes speech, summarizes the conversation into bullet points, and emails the summary via AWS SES when the user says “email me a summary.”

## Current State
- A minimal FastAPI backend and static web frontend are scaffolded.
- The app records audio, transcribes it with OpenAI, and triggers SES email sending on the voice phrase.
- SES is set up in AWS **US West (Oregon)** (`us-west-2`).
- SES is still in **sandbox**; recipients must be verified.
- Verified sender identity is the domain `pgntrain.com` (exact sender email still needed).

## Files Added
- `server/app.py`: FastAPI backend with `/transcribe` and `/summarize_email` endpoints
- `server/requirements.txt`
- `server/.env.example`
- `web/index.html`, `web/app.js`, `web/style.css`
- `README.md`

## Required Configuration (Pending)
Fill `server/.env` with:
- `OPENAI_API_KEY`
- `AWS_REGION=us-west-2` (SES is in us-west-2; ensure `.env` is not `us-east-1`)
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `SES_FROM_EMAIL` (verified sender email at `pgntrain.com`)
- `DEFAULT_TO_EMAIL` (verified recipient email, same domain)

Note: Treat `server/.env` as the source of truth for environment values. If it conflicts with this file, resolve the mismatch by updating `server/.env` first and then reflect it here.

## Open Questions
1. Which verified sender email at `pgntrain.com` should be used for `SES_FROM_EMAIL`?
2. Will you use an IAM user (static keys) or an IAM role (recommended for AWS-hosted deploys)?
3. After deciding the above, update `STATUS.md` with the chosen sender email and credential source so the next session starts with a complete config.

## Next Steps
1. Provide `SES_FROM_EMAIL` and IAM credentials source, then record them in this file.
2. Populate `server/.env`.
3. Run server and test voice capture + email.
4. If testing on Android over the network, add HTTPS (tunnel or deploy).

## Reminder
- If you see OpenAI `insufficient_quota` (HTTP 429), enable API billing and add credits in the OpenAI API platform. ChatGPT Plus does not include API usage.

## OpenAI Billing Runbook
1. Sign in to the OpenAI API platform (not ChatGPT).
2. Open Billing, add a payment method, and buy credits.
3. Wait a couple minutes for the balance to update, then retry the app.
