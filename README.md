# Aeh — Telegram Request & Verification Bot

Telegram bot for a configurable 10-program menu, user-submitted wallet/payment proof requests, and an in-Telegram admin panel.

## Features
- Ten editable program slots (name, code, enabled/disabled).
- User flow: choose program → view configured code → submit wallet number → submit amount → upload proof image.
- The welcome image is sent once per user on their first `/start`; later `/start` messages show the menu without repeating the image.
- Admin notifications with proof photo and inline **Approve**, **Reject**, and **Reopen** actions.
- Admin dashboard, request filters, user lookup, basic statistics, program management, admin management, activity log, duplicate-request warnings, and CSV export.
- SQLite persistence; pending requests are not lost when the process restarts.
- Admin IDs and bot token are configured through environment variables.

## Setup
1. Create a bot with BotFather and copy its token.
2. Set `BOT_TOKEN`, `ADMIN_IDS` (comma-separated numeric Telegram IDs), and `OWNER_ID` (the intended owner's numeric Telegram ID, which must also appear in `ADMIN_IDS`).
3. Install dependencies with `pip install -r requirements.txt`.
4. Run `python main.py`.

For Railway, add the same variables in the service's Variables tab and use `python main.py` as the start command. Keep a persistent volume mounted at `/data` and set `DB_PATH=/data/aeh.sqlite3` so SQLite data survives redeploys. If `DB_PATH` is not set, the database is created as `aeh.sqlite3` in the current directory.

## Admin use
Send `/admin` to the bot from a configured admin account. Use **Programs** to replace placeholder names/codes, **Admins** to manage admin IDs, **Requests** to review submissions, and **Export CSV** for a report. `OWNER_ID` is the owner and is the only account allowed to add/remove other admins. Set it explicitly; removed admins are not automatically restored on restart.

## Payment verification
The amount and image are user-submitted claims, not proof that funds actually arrived. Verify the transfer independently before approving. The bot records the decision and notifies the user, but it does not move money or verify wallets automatically.

## Tests
Run `python -m unittest discover -s tests -v`.


## Vercel webhook deployment

This repository exposes a FastAPI webhook at `/telegram` (or the path configured by
`TELEGRAM_WEBHOOK_PATH`). Configure these Vercel environment variables before deploying:

- `BOT_TOKEN`: Telegram bot token.
- `ADMIN_IDS`: comma-separated numeric Telegram admin IDs.
- `OWNER_ID`: owner's numeric Telegram ID, also included in `ADMIN_IDS`.
- `TELEGRAM_WEBHOOK_SECRET`: a long random secret used to validate Telegram's webhook header.
- `TELEGRAM_WEBHOOK_PATH`: optional private-looking path segment; defaults to `telegram`.
- `DATABASE_URL`: **not yet used by this SQLite-based version**. The current database layer still requires a persistent writable SQLite path and is not suitable for Vercel's ephemeral filesystem.

On Vercel startup, the app automatically registers Telegram's webhook at the stable production domain (`VERCEL_PROJECT_PRODUCTION_URL`, falling back to `VERCEL_URL`) and the configured path. It uses `TELEGRAM_WEBHOOK_SECRET` as Telegram's `secret_token`; the endpoint checks Telegram's `X-Telegram-Bot-Api-Secret-Token` header. No separate manual `setWebhook` command is required. Ensure all required environment variables are configured for Production, then redeploy.




## Neon PostgreSQL

Set `DATABASE_URL` in Vercel to the Neon PostgreSQL connection string (prefer Neon's pooled connection string for serverless). When `DATABASE_URL` is present, the bot creates and uses its PostgreSQL tables; otherwise it continues to use SQLite locally. Also configure `BOT_TOKEN`, `ADMIN_IDS`, `OWNER_ID`, and `TELEGRAM_WEBHOOK_SECRET`. Keep all secrets in Vercel Environment Variables, never in Git.

Existing SQLite records are not automatically copied to Neon; migrate/import them before switching production traffic. The current multi-step conversation drafts stored in Telegram `context.user_data` remain in process memory, so this database change persists business records but does not yet guarantee drafts survive across serverless invocations. A live Neon connection and Vercel deployment were not testable from this repository update alone.
