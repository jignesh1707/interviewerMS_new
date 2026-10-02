# Deploying on Fly.io with Supabase Postgres and Upstash Redis

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
  DEEPSEEK_API_KEY='...' OPENROUTER_API_KEY='...'
```

To sell fixed interview packs, also set `PLANS_ENABLED=true` and edit `backend/plans.yaml` (allowed lengths, minutes
per student, question counts). Student minutes live in the `quotas` table in the same Postgres schema, which the
service creates on start. See section 4b of `INTEGRATION.md`.

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

## 5. Not covered here yet

- Speech to text still runs inside the API process. At higher load, move it to its own Fly app with larger CPU
  machines.
- Webhooks are still sent from inside the request process and retried a few times. A database-backed outbox would
  make delivery durable across restarts.
- The final report is still generated inside the `finish` request (10 to 30 seconds).
