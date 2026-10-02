# Staging deploy runbook

Do this once on a staging Fly app, with a staging Supabase schema, before touching production. It follows the
testing ladder: provider keys, voice, a full interview, load, a mid-build restart, webhooks. Setup details (SQL for the
database role, Upstash, secrets) are in [DEPLOY_FLY_SUPABASE.md](DEPLOY_FLY_SUPABASE.md); this page is the order of
work and what to check at each step.

Nothing here has been run against Fly yet. What was verified locally: the Docker image (voice round trip, `/ready`
gating), the whole interview flow with a fake AI (`scripts/scripts/staging_smoke.py`, 8 students at once), and the
test suite on SQLite and Postgres 16.

## 0. Decide before you start

| Decision | Notes |
|---|---|
| Region | Next to the Supabase project (every query is a network call). `fly.toml` says `iad` as a placeholder. |
| App name | `fly.toml` says `interviewer-ms`. Use `interviewer-ms-staging` for staging with `-a`, and keep the file as is. |
| Rate limits | The whole main app is one tenant. `fly.toml` sets `RATE_LIMIT_EXPENSIVE_PER_MINUTE=300` and `DAILY_EXPENSIVE_BUDGET=20000` as starting values; the defaults (20 a minute, 2000 a day) allow about one interview a minute for the whole platform. One interview is roughly 10 to 20 AI-backed calls. The daily budget is also your cost ceiling. Size both from step 5. |
| `RETENTION_DAYS` | 0 keeps data forever. Set the real value (it must match the privacy policy). |
| `CORS_ORIGINS` | Required in production, `*` is refused. The service is private so browsers do not call it, but set the main app's origin. |
| `CALLBACK_ALLOWED_HOSTS` | The main app's public host (webhooks go to its public HTTPS URL, not the private network). |

## 1. Accounts and secrets you need

- Supabase: a **staging** schema and role (DEPLOY_FLY_SUPABASE.md, section 1). Use the transaction pooler URL.
- Upstash Redis in the same region (section 2).
- Provider keys for every provider your plans use: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`
  (and/or `OPENROUTER_API_KEY`). Staging keys with a spending cap if the providers allow it.
- Two random secrets: the service API key for the main app and `WEBHOOK_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 2. Create the app and deploy

Run from `backend/backend`. `--no-public-ips` keeps the service off the internet (the main app is in the same Fly org).

```bash
fly apps create interviewer-ms-staging
fly secrets set -a interviewer-ms-staging \
  ENVIRONMENT=production \
  API_KEYS='resumetojob:<32+ random chars>' \
  CORS_ORIGINS='https://<main app origin>' \
  CALLBACK_ALLOWED_HOSTS='<main app host>' \
  WEBHOOK_SECRET='<32+ random chars>' \
  DATABASE_URL='postgresql://interviewer_app.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require' \
  REDIS_URL='rediss://default:<token>@<name>.upstash.io:6379' \
  ANTHROPIC_API_KEY='...' OPENAI_API_KEY='...' DEEPSEEK_API_KEY='...'
fly deploy -a interviewer-ms-staging --no-public-ips
fly ips allocate-v6 --private -a interviewer-ms-staging
fly status -a interviewer-ms-staging
```

Expect two machines, both with passing checks. The first start of a machine loads the speech model in the background:
`/api/v1/ready` answers 503 ("speech model is still loading") until it is done, then 200. A cold start can take up to
a minute; if checks stay red much longer, read `fly logs`.

To reach a private app from your laptop, open a tunnel in a second terminal and use `localhost:8080` below:

```bash
fly proxy 8080:8080 -a interviewer-ms-staging
```

## 3. Check each layer

```bash
curl localhost:8080/api/v1/ready
curl -H "X-API-Key: <key>" localhost:8080/api/v1/ready/details
```

`ready/details` must show `"database": "postgres"`, `"redis": "ok"` (not `unavailable`), `stt_model_loaded: true`,
`tts_binary_available: true`, and `true` for every provider you set a key for.

Then check the provider settings and routing for both plans (this makes real, cheap calls):

```bash
fly ssh console -a interviewer-ms-staging -C "python -m app.llm_smoke --profile economy"
fly ssh console -a interviewer-ms-staging -C "python -m app.llm_smoke --profile premium"
```

The Anthropic model names and the OpenAI model names and prices in `models.yaml` were never verified against the live
APIs; this is where you find out. A wrong model name shows up here as a failed provider, not in production.

## 4. One full interview, by voice

```bash
python scripts/scripts/staging_smoke.py --base-url http://localhost:8080 --api-key <key> --audio
```

Passing means: a pack was bought and a replay did not add minutes twice, the interview was booked and debited, the
first answer was spoken (Piper) and transcribed (Whisper), `finish` answered 202 at once, the report appeared by
polling, a second `finish` returned the same saved report, the refund went through, and the student's data was erased.
The `ai=` line tells you whether real providers answered. Read the report it prints the score for: open the same
interview with `--keep` and look at the text, because the script cannot judge whether the advice is any good.

Do this for **both** plans (`--plan economy`, `--plan premium`) and compare the reports on the same answers. This is
the Economy versus Premium check from the plan; it needs a human reading the output.

## 5. Load

```bash
python scripts/scripts/staging_smoke.py --base-url http://localhost:8080 --api-key <key> --students 10
python scripts/scripts/staging_smoke.py --base-url http://localhost:8080 --api-key <key> --students 25
```

These use real providers, so each student costs a few cents. Watch:

- **Time to report** (printed at the end). It should stay well under a minute; the `finish` call itself must stay
  near zero.
- **429 responses.** The tenant limits are the first thing to hit. Raise the secrets/env values, redeploy, rerun.
- **`stt_busy` in `fly logs`** and 503s with `Retry-After`: speech recognition is saturated on a machine
  (`STT_MAX_CONCURRENT`, default 2 per machine). Add machines or CPUs. Note the smoke script answers by text except the
  first answer, so it understates speech load; for a real speech test run several `--audio` students at once.
- Machine memory and CPU in `fly dashboard` or `fly machine status`.

## 6. Kill a machine in the middle of a report

This proves the sweeper. In one terminal start a slow-ish run, and while a report is processing restart the machine:

```bash
python scripts/scripts/staging_smoke.py --base-url http://localhost:8080 --api-key <key> --students 5 --keep
fly machine list -a interviewer-ms-staging
fly machine restart <id> -a interviewer-ms-staging
```

An interrupted build is picked up by the other machine after `FINISH_STALE_SECONDS` (300, so up to about 6 minutes)
and the report appears; the script's `--report-timeout` (180 s) is shorter, so use `--report-timeout 600` for this
test. Lower `FINISH_STALE_SECONDS` in staging if you do not want to wait.

## 7. Webhooks

Point `callback_url` (or `WEBHOOK_URL`) at a receiver you control, for example a staging route on the main app or any
HTTPS request catcher. Check the signature on the receiver, then in the Supabase SQL editor:

```sql
SELECT status, count(*) FROM interviewer.webhook_outbox GROUP BY 1;
SELECT id, event, attempts, next_attempt_at, last_error FROM interviewer.webhook_outbox
 WHERE status <> 'delivered' ORDER BY id DESC LIMIT 20;
```

Take the receiver down, finish an interview, bring it back, and confirm the row goes from `pending` (with
`last_error`) to `delivered` on a later attempt and that the receiver sees the same `X-Interview-Delivery` twice if it
answered 5xx the first time. `dead` rows are webhooks the service gave up on.

## 8. What "staging passed" means

- Steps 3 to 7 pass, and the economy/premium reports read as acceptable.
- The tenant limits are sized from step 5 and written into `fly.toml` (or secrets) for production.
- You have looked at the provider spend for the load run and it matches your price per pack.
- The main app side exists and has been pointed at staging: it accepts 200 or 202 from `finish`, deduplicates on
  `X-Interview-Delivery`, polls for interviews it is still waiting on, calls `/packs/activate` from the verified
  Stripe webhook, and calls `/packs/revoke` on refunds.

## Roll back

```bash
fly releases -a interviewer-ms-staging
fly deploy -a interviewer-ms-staging --image <previous image from fly releases --image>
```

The database schema only ever gains tables and columns, so an older image runs against a newer schema.

## Not covered

- Speech to text runs inside the API process. If step 5 shows speech saturating machines, move it to its own Fly app
  with dedicated CPUs. A hosted speech API would send audio to another vendor and break the "audio never leaves the
  server" statement in DATA_HANDLING.md.
- Legal review of DATA_HANDLING.md section 9 before real students are used.
