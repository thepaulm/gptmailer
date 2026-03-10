# AGENTS.md

## Startup Instructions
- Always read `STATUS.md` first to pick up where we left off.
- If `STATUS.md` has open questions or reminders, address them early in the session.
- After reading `STATUS.md`, immediately send a short "Next Steps" checklist before waiting for user input.
- Start a persistent shell session and source the virtualenv at `~/py3` before running commands, so `python` is available.

## Working Style
- Keep changes minimal and pragmatic.
- Prefer explicit, testable steps.
- Ask before making large or irreversible changes.
- Never print or paste full secret values from `server/.env` in responses; mask sensitive values when validating config.
- After backend code changes in `server/app.py`, restart the running server before re-testing behavior.
