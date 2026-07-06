# Deploying WC Pundit (Render + Neon, both free)

This deploys `python -m app.main serve` -- the Discord bot, the TxLINE
ingestion pipeline, and the FastAPI status page -- as one Docker container
on Render's free web-service tier, with a free Neon Postgres database for
durable storage (Render's free tier has no persistent disk, so SQLite alone
wouldn't survive a redeploy or a sleep/wake cycle there).

No credit card should be required anywhere in this flow on either platform's
free tier at the time of writing -- but pricing/free-tier terms change, so
double-check both dashboards as you go and stop if either asks for payment
details.

## Prerequisites

- The GitHub repo is already pushed (`AditiChaudharyy14/worldcup-pundit`).
- Local `onboarding/credentials.json` exists (from running onboarding once).
- Your root `.env` already has `ANTHROPIC_API_KEY`, `DISCORD_BOT_TOKEN`,
  `DISCORD_CHANNEL_ID`, `DISCORD_GUILD_ID` working locally -- you'll copy
  these same values into Render, not generate new ones.

## Step 1 -- Create a free Neon Postgres database

1. Go to https://neon.tech and sign up (GitHub login is fine).
2. Create a new project (any region close to you is fine).
3. On the project's dashboard, find the **connection string** -- it looks
   like `postgresql://<user>:<password>@<host>/<dbname>?sslmode=require`.
4. Copy the whole string. This is your `DATABASE_URL` value -- treat it as
   a secret (it embeds a password).

## Step 2 -- Get a permanent Discord invite link

1. In Discord, open **WC Pundit Test** -> Server Settings -> Invites ->
   Create Invite.
2. Set **Expire after** to **Never**, **Max uses** to **No limit**.
3. Copy the invite URL (`https://discord.gg/XXXXXXX`). This is your
   `DISCORD_INVITE_URL` -- not a secret, it's what judges click to join.

## Step 3 -- Gather the credentials.json content

1. Open `onboarding/credentials.json` locally.
2. Copy its **entire contents** (the whole JSON object). This is your
   `TXLINE_CREDENTIALS_JSON` value -- treat it as a secret (it contains the
   TxLINE JWT and API token). It's copied as-is because the API token is
   long-lived for the whole paid subscription; the app refreshes only the
   short-lived JWT automatically on 401 (see `app/txline_auth.py`), which
   needs no signing and can't fail this way.

## Step 4 -- Create the Render web service

1. Go to https://render.com and sign up (GitHub login recommended).
2. **New +** -> **Web Service**.
3. Connect your GitHub account if prompted, then select the
   `worldcup-pundit` repo.
4. Render should auto-detect the `Dockerfile` at the repo root and set
   **Environment** to `Docker`. If it offers a "Runtime"/"Environment"
   dropdown, pick **Docker** explicitly.
5. **Instance Type**: select **Free**.
6. **Name**: anything, e.g. `wc-pundit`.
7. Don't click Create yet -- add the environment variables first (Step 5).

## Step 5 -- Set environment variables

In the same Render service-creation screen (or afterward, under
**Environment**), add these. Mark anything with (secret) as a Render
"secret" value if the UI distinguishes them:

| Variable | Value | Secret? |
|---|---|---|
| `ANTHROPIC_API_KEY` | same as your local `.env` | yes |
| `DISCORD_BOT_TOKEN` | same as your local `.env` | yes |
| `DISCORD_CHANNEL_ID` | same as your local `.env` | no |
| `DISCORD_GUILD_ID` | same as your local `.env` | no |
| `DISCORD_INVITE_URL` | from Step 2 | no |
| `DATABASE_URL` | from Step 1 | yes |
| `TXLINE_CREDENTIALS_JSON` | from Step 3 | yes |

Do **not** set `DATA_DIR` or `WALLET_PRIVATE_KEY` -- Render has no
persistent disk (Postgres replaces it) and the wallet key is only needed
for the one-time onboarding activation flow, not for running the deployed
app (see `app/wallet_auth.py`'s docstring for why).

Render also injects `PORT` itself; the app already reads it
(`app/main.py`'s `run_serve()`), so no action needed there.

## Step 6 -- Deploy

1. Click **Create Web Service**. Render builds the Docker image and starts
   `python -m app.main serve`.
2. Watch the build/deploy logs in Render's dashboard. You should see the
   same startup sequence as the local rehearsal: slash commands synced,
   Discord gateway connected, fixtures tracked, Uvicorn running.
3. Once live, Render shows your public URL, e.g.
   `https://wc-pundit.onrender.com`.
4. Visit `<url>/health` -- expect `{"status":"ok","discord_online":true,...}`.
5. Visit `<url>/` -- the status page should load, showing the same layout
   you saw locally.

If `discord_online` is `false` or the page 500s, check the Render logs first
-- almost always a missing/mistyped env var (check `TXLINE_CREDENTIALS_JSON`
is valid single-blob JSON with no accidental line breaks added by copy-paste).

## Step 7 -- Keep it awake: UptimeRobot

Render's free web services spin down after ~15 minutes with no incoming HTTP
requests (background work like the Discord/TxLINE connections doesn't count
as "activity" for this) and take ~30-60s to cold-start back up on the next
request. Since judges expect the link to work instantly, and the bot should
stay connected to Discord continuously:

1. Go to https://uptimerobot.com and sign up free.
2. **Add New Monitor**.
   - Monitor Type: **HTTP(s)**
   - Friendly Name: `WC Pundit`
   - URL: `https://<your-render-url>/health`
   - Monitoring Interval: **5 minutes** (the free tier's minimum, and
     comfortably under Render's 15-minute spin-down window).
3. Save. UptimeRobot will now ping `/health` every 5 minutes, keeping the
   service warm.

### What happens if it sleeps anyway

If a sleep/wake cycle does happen (e.g. before UptimeRobot is set up, or a
gap in monitoring), Render fully restarts the container on the next request
-- this is a full process restart, not a pause/resume. On restart:

- The Discord bot does a fresh login/gateway connect (already handled --
  `discord.py`'s own reconnect logic, nothing extra needed).
- `bot.py`'s `setup_hook` re-fetches today's fixtures and re-registers
  (`Client.add_view()`) any still-open Hi-Lo question buttons from Postgres,
  regardless of which process last posted them (see `game.py`/`storage.py`
  -- this was built and verified during Phase 4 for exactly this kind of
  restart).
- TxLINE's SSE streams (scores/odds) and the guest-JWT auth all reconnect
  with their own existing backoff logic (`txline_client.py`,
  `txline_auth.py`).
- Nothing needs to be done manually; the only real cost is missing whatever
  live events occurred in the gap between the old connection dying and the
  new one starting.

## Updating the deployment later

Render redeploys automatically on every push to the connected branch (or
you can trigger a manual deploy from the dashboard). Since Postgres is
external (Neon), your data (leaderboard, streaks, idempotency ledger)
survives redeploys -- that's the whole reason it's not on local SQLite here.

## Troubleshooting

- **Build fails**: check the Dockerfile builds locally first --
  `docker build -t wc-pundit .` from the repo root.
- **App crashes on startup with a credentials error**: `TXLINE_CREDENTIALS_JSON`
  is missing, empty, or not valid JSON -- re-copy `onboarding/credentials.json`'s
  raw contents exactly.
- **Discord bot never comes online**: double check `DISCORD_BOT_TOKEN` and
  that the bot is still a member of "WC Pundit Test" with the Message
  Content intent enabled in the Discord Developer Portal.
- **Postgres connection errors**: confirm `DATABASE_URL` includes
  `?sslmode=require` (Neon requires SSL) and that you copied the full
  string including the password.
