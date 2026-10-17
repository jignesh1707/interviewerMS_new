# Secrets map (names only, never values)

Every secret value lives in **Bitwarden**. This file says which secrets exist, where each one is set, and which ones
must hold the same value as another. It never contains a value. Keep it in step whenever a secret is added or removed;
`fly-secrets.ps1 -Action Check` shows the drift.

## How the values are kept in Bitwarden

| Bitwarden folder | Secure note | Holds |
|---|---|---|
| `RTJ Production` | `fly/interviewer-ms` | the Fly secrets of the production Interviewer service |
| `RTJ Production` | `fly/resumetojob-api` | the Fly secrets of the production Resumeto API (app and worker) |
| `RTJ Production` | `fly/resumetojob-tailoring` | the Fly secrets of the production tailoring service |
| `RTJ Staging` | `fly/interviewer-ms-staging` | staging Interviewer service |
| `RTJ Staging` | `fly/resumetojob-api-staging` | staging Resumeto API |

One **hidden custom field per secret**; the field name is the exact variable name (`DATABASE_URL`). Account logins
(Fly, Stripe, Supabase, Upstash, Clerk, Vercel, OpenAI, Anthropic, OpenRouter, Resend) are ordinary Bitwarden login
items in the same folders. Never put a staging value in the production folder or the other way round.

## The script: `fly-secrets.ps1`

One-time setup (PowerShell):

```powershell
winget install Bitwarden.CLI
bw login                                  # once per computer
$env:BW_SESSION = bw unlock --raw         # each new terminal; locks again when the terminal closes
```

| Action | Example | What it does | Shows values? |
|---|---|---|---|
| `Capture` | `.\fly-secrets.ps1 -Action Capture -App interviewer-ms` | Reads the app's secrets from a running machine and writes them into its Bitwarden note (creates or updates it) | Never |
| `Check` | `.\fly-secrets.ps1 -Action Check -App interviewer-ms` | Names in Fly but not in Bitwarden, and the reverse | Never |
| `Verify` | `.\fly-secrets.ps1 -Action Verify -App interviewer-ms` | Each value in Bitwarden vs on the live machine: `match` or `DIFFERENT` | Never |
| `Pairs` | `.\fly-secrets.ps1 -Action Pairs -Env prod` | The cross-app pairs below, from Bitwarden: `match` or `DIFFERENT` | Never |
| `Push` | `.\fly-secrets.ps1 -Action Push -App interviewer-ms` | Bitwarden values into Fly with `--stage` (nothing restarts until the next deploy or `fly secrets deploy`) | Never |

`Capture` and `Verify` need one machine of the app **started** (staging machines are usually stopped:
`fly machine start <id> -a <app>`). They read the machine's environment over `fly ssh`; values stay in memory and go
straight to Bitwarden or into a match / DIFFERENT comparison. Nothing is written to disk or to the screen.

**Changing a secret:** change the field in Bitwarden, `Push` that app, deploy it, then `Verify`. If the secret is in a
pair, change both sides, then run `Pairs`.

## Pairs that must match

| What | Production | Staging |
|---|---|---|
| Interviewer API key | `interviewer-ms` `API_KEYS` contains `resumetojob:<key>` and `resumetojob-api` `RTJ_INTERVIEWER_API_KEY` = `<key>` | same with the `-staging` apps |
| Interviewer webhook secret | `interviewer-ms` `WEBHOOK_SECRET` = `resumetojob-api` `RTJ_INTERVIEWER_WEBHOOK_SECRET` | same with the `-staging` apps |
| Callback host | the host of `RTJ_INTERVIEWER_CALLBACK_URL` is listed in `interviewer-ms` `CALLBACK_ALLOWED_HOSTS` | same with the `-staging` apps |
| Tailoring token | `resumetojob-tailoring` `TAILORING_JWT_SECRET` = `resumetojob-api` `RTJ_TAILORING_JWT_SECRET` | (no staging tailoring app) |
| Stripe webhook signing secret | Stripe **live** endpoint "ResumeToJob Production Backend" = `RTJ_STRIPE_WEBHOOK_SECRET` | Stripe **sandbox** endpoint = staging `RTJ_STRIPE_WEBHOOK_SECRET` |
| Interviewer DB password | Supabase role `interviewer_app` = the password inside `interviewer-ms` `DATABASE_URL` | role `interviewer_staging_app` inside staging `DATABASE_URL` |

The last two have one side outside Fly, so `Pairs` cannot check them; when you roll either one, update both places.

## Never change

| Secret | Why |
|---|---|
| `RTJ_INTERVIEWER_REF_PEPPER` (prod and staging, different values) | It keys every student's interview wallets and erasure. A new value orphans every balance. Keep a second, offline copy. |

## Secrets per app

Secret values are marked **S**; the rest are plain settings kept in Fly secrets for convenience (still captured, so a
rebuild has everything).

### `interviewer-ms` (production) and `interviewer-ms-staging`

| Name | | Notes |
|---|---|---|
| `API_KEYS` | S | `resumetojob:<key>`; pair with `RTJ_INTERVIEWER_API_KEY` |
| `WEBHOOK_SECRET` | S | pair with `RTJ_INTERVIEWER_WEBHOOK_SECRET` |
| `DATABASE_URL` | S | Supabase, role `interviewer_app` (staging: `interviewer_staging_app`), host `db.<ref>.supabase.co:6543`, plain user, `?sslmode=require` |
| `REDIS_URL` | S | production only: `rediss://` with the Upstash **TCP** password (not the REST token); shares `rtj-prod-redis` with the API |
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` | S | production and staging currently share the OpenAI and Anthropic keys |
| `ENVIRONMENT`, `CORS_ORIGINS`, `CALLBACK_ALLOWED_HOSTS` | | |
| `DB_SCHEMA`, `FINISH_STALE_SECONDS`, `LLM_TIMEOUT_SECONDS` | | staging only |

### `resumetojob-api` (production) and `resumetojob-api-staging`

| Name | | Notes |
|---|---|---|
| `RTJ_DATABASE_URL`, `RTJ_REDIS_URL` | S | |
| `RTJ_SUPABASE_URL` / `RTJ_SUPABASE_SERVICE_ROLE_KEY` / `RTJ_SUPABASE_STORAGE_BUCKET` | S (key) | URL without `/rest/v1/` |
| `RTJ_CLERK_SECRET_KEY` | S | plus `RTJ_CLERK_ISSUER`, `RTJ_CLERK_JWKS_URL`, `RTJ_CLERK_PUBLISHABLE_KEY` (prod), `RTJ_CLERK_AUTHORIZED_PARTIES` (staging) |
| `RTJ_STRIPE_SECRET_KEY` | S | live `sk_live_` in prod, `sk_test_` in staging |
| `RTJ_STRIPE_WEBHOOK_SECRET` | S | pair with the Stripe endpoint |
| `RTJ_STRIPE_PRICE_ID_WEEKLY` / `_MONTHLY` / `_QUARTERLY`, `RTJ_STRIPE_PRICE_ID_VOICE_ADDON_5` / `_10_ECONOMY` / `_10_PREMIUM` / `_20_ECONOMY` / `_20_PREMIUM` | | not secret; differ between live and sandbox |
| `RTJ_INTERVIEWER_API_KEY`, `RTJ_INTERVIEWER_WEBHOOK_SECRET` | S | pairs, see above |
| `RTJ_INTERVIEWER_REF_PEPPER` | S | **never change** |
| `RTJ_INTERVIEWER_ENABLED`, `RTJ_INTERVIEWER_BASE_URL`, `RTJ_INTERVIEWER_CALLBACK_URL` | | `http://interviewer-ms.flycast`, `https://resumetojob-api.fly.dev/voice-interview/webhook` |
| `RTJ_LOG_PEPPER` | S | changing it only breaks log correlation |
| `RTJ_TAILORING_JWT_SECRET` | S | prod only; pair with the tailoring service |
| `RTJ_ANTHROPIC_API_KEY`, `RTJ_VOYAGE_API_KEY`, `RTJ_RESEND_API_KEY`, `RTJ_ADZUNA_APP_KEY`, `RTJ_TELEGRAM_BOT_TOKEN`, `RTJ_TELEGRAM_WEBHOOK_SECRET`, `RTJ_SPONSOR_DIRECTORY_PASSWORD` | S | prod only |
| `RTJ_ENV`, `RTJ_PUBLIC_API_BASE_URL`, `RTJ_PUBLIC_WEB_BASE_URL`, `RTJ_CORS_ALLOW_ORIGINS`, `RTJ_STORAGE_BACKEND`, `RTJ_LOG_LEVEL` | | `RTJ_PUBLIC_WEB_BASE_URL` must be the live site (`https://www.resu2job.us`), or Checkout returns to `localhost` |
| everything else (`RTJ_ADMIN_USER_IDS`, `RTJ_ADZUNA_*`, `RTJ_JOB_SOURCE`, `RTJ_MATCH_SCORE_*`, `RTJ_SPONSOR_*`, `RTJ_*_ENABLED`, `RTJ_*_SOURCE`, `RTJ_*_TTL_SECONDS`, `RTJ_DAILY_DIGEST_MAILING_ADDRESS`, `RTJ_TELEGRAM_BOT_USERNAME`, `RTJ_COST_PERSIST_ENABLED`, `RTJ_TAILORING_URL`, `RTJ_TAILORING_ENABLED`) | | plain settings |

### `resumetojob-tailoring` (production)

| Name | | Notes |
|---|---|---|
| `TAILORING_JWT_SECRET` | S | pair with `RTJ_TAILORING_JWT_SECRET` |
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` | S | its own keys |

### Vercel (not in Fly)

`resumeto-web` (production) and `resumeto-web-staging` hold `VITE_API_URL` and `VITE_CLERK_PUBLISHABLE_KEY`. Both
end up in the browser bundle, so they are not secrets; note them in the Bitwarden folder for a rebuild.
