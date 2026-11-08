# Deploying on Fly.io with Supabase Postgres and Upstash Redis

For the order of work on a first (staging) deploy, with checks at each step, see [STAGING.md](STAGING.md).

This service is stateless once `DATABASE_URL` and `REDIS_URL` are set, so you can run several machines behind
Fly's load balancer. Nothing is stored on the machine's disk (audio is discarded unless `RETAIN_AUDIO=true`).

## 1. Database: a dedicated schema and role in your Supabase project

Never put candidate data in the `public` schema: Supabase exposes it through its automatic REST API.
Run this once in the Supabase SQL editor (replace the password):

```sql
CREATE ROLE interviewer_app LOGIN PASSWORD 'REPLACE-WITH-A-LONG-RANDOM-PASSWORD';
GRANT interviewer_app TO postgres;                       -- lets the SQL editor create objects for it
CREATE SCHEMA interviewer AUTHORIZATION interviewer_app;
```

- The role owns only the `interviewer` schema. It has no access to your main app's tables, and the main app's
  roles (`anon`, `authenticated`) have none to this one.
- Leave `interviewer` out of **Settings, API, Exposed schemas**. Only `public` should be there.
- The tables are created by the service on first start (`DB_AUTO_MIGRATE=true`, the default) with row level
  security switched on and no policies. As the owner, the app role is unaffected, and any other role sees nothing
  even if someone grants it access by mistake.
- To run the schema SQL yourself instead: `python -m app.migrate --print`, review it, run it as the role, then
  set `DB_AUTO_MIGRATE=false`.

**Connection string.** In the Supabase dashboard open Connect, then Transaction pooler, and copy the host. Use
your new role, not `postgres`. The pooler user name includes the project reference:

```
postgresql://interviewer_app.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

The service connects without server-side prepared statements, which the transaction pooler requires.
Keep `DB_POOL_SIZE` at 5 or lower per machine so several machines stay under your plan's pooler limit.

## 2. Redis: Upstash

Create the database in the same region as your Fly app and copy the TLS URL:

```
REDIS_URL=rediss://default:<token>@<name>.upstash.io:6379
```

It stores rate-limit counters and the per-tenant daily budget, shared by all machines. If Redis is unreachable the
service keeps working with per-machine limits and logs a warning, and `/api/v1/ready/details` shows
`"redis": "unavailable"`. Cost is about one command per request.

## 3. Fly

Pick a Fly region close to your Supabase region, because every query is a network call.

```bash
fly apps create interviewer-ms
fly secrets set \
  ENVIRONMENT=production \
  API_KEYS='resumetojob:<32+ random chars>' \
  CORS_ORIGINS='https://app.example.com' \
  WEBHOOK_SECRET='<32+ random chars>' \
  CALLBACK_ALLOWED_HOSTS='app.example.com' \
  DATABASE_URL='postgresql://interviewer_app.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require' \
  REDIS_URL='rediss://default:<token>@<name>.upstash.io:6379' \
  ANTHROPIC_API_KEY='...' OPENAI_API_KEY='...'
```

To sell interview packs, also set `PLANS_ENABLED=true` and edit `backend/plans.yaml` (pack size and days, lengths,
refund rule) and `backend/models.yaml` (which AI providers serve each plan). Students' packs and payments live in the
`packs` and `pack_payments` tables in the same Postgres schema, which the service creates on start. Add the keys for
every provider your plans use (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`), set `RETENTION_DAYS`,
and run `python -m app.llm_smoke --profile premium` (and `economy`) once to check them. See section 4b of
`INTEGRATION.md` and `DATA_HANDLING.md`.

Settings to know about:

- **No public address.** Call the service from the main app over Fly's private network. Do not add a public
  `[http_service]` to `fly.toml`. Check whether your app must listen on `::` for `.internal` names or use `.flycast`.
- **Webhooks go to the main app's public HTTPS URL.** The callback check rejects private addresses, which includes
  Fly's internal network, so list the public host in `CALLBACK_ALLOWED_HOSTS`.
- Health check path: `/api/v1/health`. Readiness (database reachable): `/api/v1/ready`.
- Run at least two machines. Because state is in Postgres and Redis, any machine can serve any request.
- The `CORS_ORIGINS` value is required in production, and `*` is rejected.

## 4. Erasing a student's data

Send your own user id as `external_ref` when creating an interview, then:

```
GET    /api/v1/interviews?external_ref=<user id>        list a student's interviews
DELETE /api/v1/interviews/by-ref/<user id>              erase all of them: answers, reports, events, audio
```

Supabase keeps daily backups, so erased rows can remain in backups until they expire. Mention that in your privacy
policy.

## 5. Asynchronous reports and the webhook outbox

Set `FINISH_ASYNC=true` in production. `POST .../finish` then answers 202 immediately and the report is built in the
background, so no proxy timeout can cut off a student's request. Any machine can build it; the status change to
`processing` is a single conditional UPDATE, so two finish calls (or two machines) never build it twice. A sweeper on
every machine takes over builds that have shown no progress for `FINISH_STALE_SECONDS` (300), which covers deploys and
crashes. The main app polls `GET .../report` and/or waits for the `interview.completed` webhook (INTEGRATION.md).

Webhooks go through the `webhook_outbox` table and are retried with backoff until delivered (INTEGRATION.md,
section 5). Two things to watch: rows with `status = 'dead'` are webhooks that were given up on, and `pending` rows
that keep growing mean the receiver is down. Both are plain SQL (`SELECT status, count(*) FROM interviewer.webhook_outbox
GROUP BY 1`).

A build cut off by a deploy or crash is repeated from the start, so its AI calls are paid for again each time its
machine is interrupted. That should be rare; if machines keep dying mid-build (for example out of memory), fix that first.

## 6. Not covered here yet

- Speech to text still runs inside the API process. At higher load, move it to its own Fly app with larger CPU
  machines.

## 7. The light instance (`interviewer-ms-light`)

A small, always-on Interviewer that answers only cheap reads (a student's packs and minutes, plans, health, ready) so those
reads do not wake the big speech machines. Config: `backend/backend/fly.light.toml`. Which calls it serves and refuses, and
what the main app changes: `INTEGRATION.md` section 4c.

**It must be a SEPARATE Fly app.** Do not add a small machine to `interviewer-ms`. Fly's load balancer sends a request to any
machine of an app, so a small machine inside the existing app would receive speech uploads (and the big ones would still wake
for reads). `LIGHT_MODE=true` also refuses those uploads with 404 `light_mode_route`, but that is a safety net, not the design.

Create it once (run from `backend/backend`; nothing here deploys the main app):

```bash
fly apps create interviewer-ms-light
fly secrets set -a interviewer-ms-light --stage API_KEYS=... DATABASE_URL=... REDIS_URL=...   # values from Bitwarden, see below
fly deploy -c fly.light.toml --no-public-ips --build-arg GIT_SHA=$(git rev-parse HEAD)
fly ips allocate-v6 --private -a interviewer-ms-light   # only if the main app reaches the main service through .flycast
fly status -a interviewer-ms-light                      # one machine, started, 2 checks passing
```

Secrets the light app needs (names only; copy the **values** from the `interviewer-ms` production note in Bitwarden, never
into a file or a chat):

| Secret | Needed? | Notes |
|---|---|---|
| `API_KEYS` | yes | the same value as the main app, so `RTJ_INTERVIEWER_API_KEY` works at both addresses |
| `DATABASE_URL` | yes | the same database and role as the main app: the light app reads the same packs |
| `REDIS_URL` | recommended | with it, rate-limit counters are shared with the main app; without it each app counts separately |
| `CORS_ORIGINS` | no | only the main app's backend calls it, and the default passes the startup check; set it to the main app's value if you want them identical |

Not needed on the light app (it does no AI work and sends no webhooks): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `WEBHOOK_SECRET`, `CALLBACK_ALLOWED_HOSTS`. If the main app sets `PLANS_OVERRIDES`,
set the same value on the light app so both report the same pack numbers.

Things to know:

- **Same release.** Deploy the light app from the same commit as the main app and redeploy both when you release. It runs the
  same migrations on start (`DB_AUTO_MIGRATE`), so a light app on much older code than the main app can read a schema it does
  not know.
- **Secrets map.** Record `interviewer-ms-light` next to `interviewer-ms` in `ops/secrets/README.md` (its `API_KEYS` must hold
  the same key as the main app's).
- **Check it.** `curl http://interviewer-ms-light.internal:8080/api/v1/health` from a machine on the private network answers
  `"mode": "light"`. `GET /api/v1/packs/<ref>` with the API key returns the same body as the main address. A call to
  `/api/v1/interviews` answers 404 `light_mode_route`.
- **Cost and size.** One `shared-cpu-1x` with 512 MB runs all day. Add a second machine with
  `fly scale count 2 -a interviewer-ms-light` only if one is not enough.
- **Database connections.** Each light machine opens up to `DB_POOL_SIZE` (5) connections to the shared Supabase pooler, on top of
  the big machines' connections. Count them when sizing the pool limit.
- **If it is down**, the main app falls back to the normal address (INTEGRATION.md section 4c). Nothing else depends on it.
