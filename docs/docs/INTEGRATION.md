# Integrating with the main app

The service is stateless from the caller's perspective: the main app creates an interview, submits
answers, and receives the report. Everything is persisted server-side in SQLite.

## 1. Authentication

Send the API key on every `/api/v1` request (except `/health` and `/ready`):

```
X-API-Key: <key>
```

Configure accepted keys on the service with `API_KEYS=key-one,key-two`, or `API_KEYS=resumetojob:<key>` to name the tenant.

Each key maps to a tenant. A tenant can only list, read, answer and finish interviews it created; anything else returns 404. Give resumetojob its own key.

Outside `ENVIRONMENT=development` the service refuses to start if a key is the default, shorter than 32 characters, or `CORS_ORIGINS` is `*`. Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## 2. Two integration modes

### Mode A: main app supplies resume and JD

The main app already holds candidate documents, so it sends text or files when creating the session.

```bash
curl -sX POST http://localhost:8080/api/v1/interviews \
  -H 'X-API-Key: dev-key-change-me' \
  -H 'Content-Type: application/json' \
  -d '{
        "role": "Backend Engineer",
        "candidate_name": "Jane Doe",
        "resume_text": "<resume text>",
        "jd_text": "<job description text>",
        "callback_url": "https://main-app.example.com/hooks/voice-interview",
        "metadata": {"candidate_id": "c-1024", "job_id": "j-77"},
        "config": {"question_count": 8, "ask_followups": true, "analyze_per_answer": true}
      }'
```

For file uploads use multipart:

```bash
curl -sX POST http://localhost:8080/api/v1/interviews/upload \
  -H 'X-API-Key: dev-key-change-me' \
  -F role='Backend Engineer' \
  -F candidate_name='Jane Doe' \
  -F 'config_json={"question_count":8}' \
  -F 'metadata_json={"candidate_id":"c-1024"}' \
  -F resume_file=@resume.pdf \
  -F jd_file=@jd.pdf
```

### Mode B: the student attaches documents

Point the candidate at the hosted UI and pass the API key through your own backend proxy, or embed
the same form in your app. Either way the create call is identical.

## 3. Driving the interview

The response contains `interview` (with `id` and `status`) and `questions`.

For each question:

1. Display `question.question`. Optionally play it with `POST /api/v1/speech/synthesize`
   (`text` form field, returns `audio/wav`).
2. Record the answer in the browser with `MediaRecorder`.
3. Upload it to `POST /api/v1/interviews/{id}/answers/audio` with `question_index`, `audio` and
   `duration_seconds`. The service transcribes and analyses it.
4. Show the returned `heuristic_scores`, `analysis`, and `followup`.

Typed answers use `POST /api/v1/interviews/{id}/answers` with `question_index` and `transcript`.

When done, call `POST /api/v1/interviews/{id}/finish`. What comes back depends on `FINISH_ASYNC`:

- **`FINISH_ASYNC=true` (use this in production).** The call answers **202** straight away with
  `{"status": "processing", "report": null}` and the report is built in the background (10 to 30 s). Wait for the
  `interview.completed` webhook, or poll `GET /api/v1/interviews/{id}/report` every 2 to 3 seconds until `status` is
  `completed` (then `report` is filled in) or `failed` (then `error` says why). Calling `finish` again while it is
  processing is safe: it returns 202 again and does **not** start a second build. Calling it after a failure retries.
  Calling it after completion returns **200** with the saved report.
- **`FINISH_ASYNC=false` (default, for local use).** The call waits and answers **200** with the report.

Write your client to accept both: if the response has a `report`, use it, otherwise wait as above.

## 4. Report shape

```json
{
  "interview_id": "…",
  "overall_score": 76,
  "readiness_level": "almost_ready",
  "dimension_scores": {
    "communication": 80,
    "structure": 75,
    "technical_depth": 78,
    "impact": 70,
    "role_fit": 72
  },
  "summary": "Solid structure, needs quantified impact.",
  "top_strengths": ["clear storytelling"],
  "critical_gaps": ["quantified impact"],
  "improvement_plan": {
    "quick_wins": ["Add numbers to every result"],
    "one_week_plan": ["Write five STAR stories"],
    "thirty_day_plan": ["Do two mock interviews"],
    "practice_prompts": ["Describe a latency win"]
  },
  "per_question": [
    {"index": 0, "question": "…", "overall": 78, "words_per_minute": 132, "filler_total": 3}
  ],
  "routing_trace": {
    "final_scoring": {"provider": "openai", "model": "gpt-4o", "tier": "premium", "fallbacks": 0}
  }
}
```

`routing_trace` tells you which provider and tier served each step, and how many failovers happened.

## 4b. Plans, packs and interview length

Off by default. Set `PLANS_ENABLED=true` and edit `backend/plans.yaml` and `backend/models.yaml` (no code change; restart to apply).

A student buys a **pack** of interview minutes that expires after a number of days. The main app tells this service about each payment; **this service owns the balance, the days left and the refund rule**. There are two plans in the shipped config:

| Plan | Pack | AI providers that may see the student's data |
|---|---|---|
| `economy` ($10) | 150 minutes, 30 days | DeepSeek first, then OpenRouter, OpenAI, Anthropic |
| `premium` ($20) | 250 minutes, 30 days | Anthropic, then OpenAI. **Never DeepSeek.** |

```yaml
# plans.yaml
profiles:                       # what each interview length contains
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
  25: {question_count: 12, max_followups: 3}
plans:
  premium:
    pack_minutes: 250
    pack_days: 30
    durations: [15, 20, 25]
    default_duration: 15
    llm_profile: premium                      # a profile in models.yaml
    llm_allowed_providers: [anthropic, openai]  # a hard promise, enforced on every AI call
    refund: {max_interviews_started: 1, max_minutes_used: 15, rule: any}
```

### The flow, from the main app's backend

1. **A student pays.** After you have verified Stripe's own webhook, call (from your backend, never from a browser):

   ```bash
   curl -sX POST http://localhost:8080/api/v1/packs/activate -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"external_ref": "STUDENT_HASH", "plan": "economy", "payment_id": "evt_1Abc...", "purchased_at": "2026-10-02T12:00:00Z"}'
   # {"applied": true, "pack": {"plan":"economy","active":true,"minutes_total":150,"minutes_remaining":150,
   #   "expires_at":"2026-11-01T12:00:00+00:00","days_remaining":30,"refund_eligible":true, ...}}
   ```

   - `payment_id` is your Stripe event or payment id. **A replay is applied once** (`"applied": false`), so webhook retries are safe.
   - `purchased_at` is when the student paid (with a timezone), so expiry is the same however late the call arrives.
   - **Buying again stacks.** While the pack is still active, the new minutes are added and the expiry is pushed back by another `pack_days` (no days are lost). If the old pack expired or was used up, a fresh pack starts at the purchase date and leftovers are forfeited.
   - Trial users get nothing: only call this for a real payment.
2. **The student clicks "Start interview"** in the main app. Ask the service first, to decide what to show:

   ```bash
   curl -s http://localhost:8080/api/v1/packs/STUDENT_HASH -H "X-API-Key: $KEY"
   # {"external_ref": "...", "packs": [{"plan":"economy","active":true,"minutes_remaining":135,"days_remaining":29,...},
   #                                   {"plan":"premium","active":false,...}]}
   ```

   If no plan is active, show "buy a pack". Otherwise create the interview with the `plan` the student chose. The service also refuses at create, so the check cannot be skipped.
3. **Create the interview** (`POST /api/v1/interviews`, or `/upload`) with `external_ref`, `plan` and `config.duration_minutes`. Booking debits the full length. If there is nothing to spend you get **402** `quota_exceeded` with `details.reason` of `no_active_pack`, `pack_expired`, `no_minutes_left` or `insufficient_minutes`, plus `remaining_minutes`, `expires_at` and `days_remaining`. If creation fails (for example every AI provider is down) the minutes are given back.
4. **Refund.** Ask the service, which applies the refund rule (`refund_eligible` in the balance shows the answer in advance):

   ```bash
   curl -sX POST http://localhost:8080/api/v1/packs/revoke -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"external_ref": "STUDENT_HASH", "plan": "economy", "payment_id": "evt_1Abc..."}'
   ```

   Success removes that purchase's minutes and days (stacked purchases are refunded one at a time). If the student has used the pack too much it returns **409** `refund_not_allowed` and you must not refund. A chargeback is the bank's decision and cannot be refused here; the interview records (times, minutes used) are your evidence.

The refund rule in `plans.yaml` is `rule: any`: refundable while **either** at most `max_interviews_started` interview has been started **or** at most `max_minutes_used` booked minutes are used. `rule: all` needs both. Used minutes are the booked length, not the time actually spoken.

`GET /api/v1/plans` returns the catalog (pack size, days, lengths, refund rule) and, for each plan, `llm_providers`: the companies that may process that plan's data. Use it to render pricing and the consent screen so they always match what the service really does.

### Interview length, deadline and caps

- **Student id is required.** Send `external_ref` on create (use a stable id; hashing it with a secret pepper keeps raw user ids out of this service, but never rotate the pepper or balances and erasure stop matching).
- **Choose a length** with `config.duration_minutes` (default: the plan's default). A length the plan does not allow returns 422. The length sets `question_count` and `max_followups`; values you send for those are replaced.
- **Server-side deadline.** The create response has `interview.duration_minutes` and `interview.deadline_at` (length plus `grace_seconds`, counted from when the questions are ready). After it, answers return **409** `time_limit_reached`; `finish` still works so the student gets the report. Audio is refused before any transcription work is spent. The status (create, `GET .../status`) also carries `grace_seconds` and `seconds_remaining` (to `deadline_at`, measured by the server, so a countdown does not depend on the student's clock). The nominal end to show the student is `seconds_remaining - grace_seconds`; the demo UI's countdown (`frontend/frontend/src/countdown.ts`) does exactly that.
- **Per-answer caps** (always on): `MAX_ANSWER_SECONDS` (180) and `MAX_TRANSCRIPT_CHARS` (4000). Longer answers return 422 and are not stored.

### Which AI providers see student data

- **Profiles.** `models.yaml` has an `economy` profile (the top-level `tiers`) and a `premium` profile, each with its own provider and model lists. Fallbacks never leave a profile, so a Premium interview cannot fall through to DeepSeek.
- **Hard allow-list.** A plan's `llm_allowed_providers` is passed with every AI call and the router refuses anything else, whatever `models.yaml` says. At startup the service **refuses to start** if a plan names a profile that does not exist, or its profile lists a provider the plan forbids.
- **Data minimization.** Before a resume or an answer goes to an AI provider the service removes names, emails, phone numbers, links, addresses, ID numbers and work-authorization lines (OPT, H-1B, visa sponsorship, citizenship...). `REDACT_PII_FOR_LLM=true` (default). Only the copy for the provider is changed; stored text and analytics use the original. This is best-effort minimization, **not anonymization**: employers, schools and anything a student says aloud can still identify them.
- **If you change providers, update the privacy policy and consent text in the main app.**
- Run `python -m app.llm_smoke --profile premium` (with the real keys set) to send one tiny request to each model and check keys, model names and per-model options before launch.

**Per-student rate limits** (independent of `PLANS_ENABLED`): once an interview has an `external_ref`, its LLM and speech-backed calls (create, answer, audio answer, finish) also count against that student: `STUDENT_RATE_LIMIT_PER_MINUTE` (30) and `STUDENT_DAILY_BUDGET` (200 per UTC day), `0` to turn either off. They are kept per tenant and student, shared across machines through Redis, and return 429 like the tenant limits. The standalone `/speech/transcribe` and `/speech/synthesize` calls carry no student id, so only the tenant limits cover them.

Pack rows are keyed by `external_ref` and are not removed by the erase-by-user endpoint, so erasing a student's interviews does not reset or refund their balance.

## 5. Webhooks

`callback_url` must be `https`, must not embed credentials, and must resolve only to public addresses (private, loopback and link-local targets are rejected, and redirects are not followed). Set `CALLBACK_ALLOWED_HOSTS=resumetojob.example.com` to restrict it to your own receiver. The check runs again at delivery time.

Limits (all configurable): `MAX_DOC_UPLOAD_MB` (5), `MAX_TEXT_CHARS` (100000), `MAX_TRANSCRIPT_CHARS` (4000, one answer), `MAX_ANSWER_SECONDS` (180), `MAX_METADATA_BYTES` (16384), `STT_MAX_UPLOAD_MB` (25). Oversized requests return 413 or 422.

Events:

- `interview.created` — questions are ready.
- `interview.completed` — report is stored and available.

Payload:

```json
{"event": "interview.completed", "sent_at": 1750000000.0,
 "data": {"interview_id": "…", "status": "completed", "overall_score": 76, "readiness_level": "almost_ready"}}
```

Headers:

- `X-Interview-Event`
- `X-Interview-Delivery` — unique id per delivery, use it to deduplicate retries
- `X-Interview-Timestamp` — unix seconds when the request was sent
- `X-Interview-Signature-V2` — HMAC-SHA256 of `<timestamp>.<raw body>` using `WEBHOOK_SECRET`. Verify this one and reject timestamps older than ~5 minutes to block replays.
- `X-Interview-Signature` — legacy HMAC-SHA256 of the raw body only (no replay protection)

Outside `ENVIRONMENT=development` the service will not send unsigned webhooks: set `WEBHOOK_SECRET`.

**Delivery is durable and at least once.** Each webhook is written to a database table (`webhook_outbox`) before anything is sent, and a background loop delivers it. If your receiver is down, returns 5xx or 429, or the service restarts or deploys, delivery is retried with backoff after 30 s, 2 min, 10 min, 1 h, 6 h and 12 h, then given up (`WEBHOOK_OUTBOX_MAX_ATTEMPTS`, default 8 attempts). Other 4xx answers (your receiver rejected it) are not retried. Every retry carries a fresh timestamp and signature but the same `X-Interview-Delivery`, so **deduplicate on that header**: a webhook can arrive twice. Delivered and abandoned rows are deleted after `WEBHOOK_OUTBOX_KEEP_DAYS` (7) and when the interview is erased. Because a receiver can be down for longer than that, also reconcile by polling (section 6) for interviews you are still waiting on.

Python verification:

```python
import hashlib, hmac

import time

def verify(raw_body: bytes, timestamp: str, signature_v2: str, secret: str, tolerance: int = 300) -> bool:
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_v2)
```

Node verification:

```javascript
const crypto = require('crypto')

function verify(rawBody, timestamp, signatureV2, secret, toleranceSeconds = 300) {
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > toleranceSeconds) return false
  const expected = crypto.createHmac('sha256', secret).update(`${timestamp}.`).update(rawBody).digest('hex')
  const a = Buffer.from(expected), b = Buffer.from(signatureV2)
  return a.length === b.length && crypto.timingSafeEqual(a, b)
}
```

In development only, an unset `WEBHOOK_SECRET` sends unsigned webhooks.

## 6. Polling alternative

If webhooks are not possible, poll:

- `GET /api/v1/interviews/{id}/status` — `status` moves `created` → `questions_ready` →
  `in_progress` → `processing` → `completed`.
- `GET /api/v1/interviews/{id}/report` — returns `report: null` until finished.

## 7. Embedding the UI

The demo frontend is a plain Vite app. To embed it:

1. Build it with `npm run build` in `frontend`.
2. Serve `frontend/dist` from the main app and proxy `/api` to this service.
3. Do not ship the service API key to browsers. The demo keeps a pasted key in `sessionStorage`; in
   production have your backend call this service, or proxy `/api` and attach the key server-side.

The dev server listens on loopback and proxies `/api`. Set `VITE_HOST` and `VITE_ALLOWED_HOSTS` to expose it.

## 8. Operational notes

- Audio is stored under `STORAGE_DIR/audio/{interview_id}`. Clean it up on your retention schedule.
- Reports and transcripts live in SQLite at `DATABASE_PATH`. Back up that file.
- Degraded model calls are recorded as `interview.llm_degraded` events, visible at
  `GET /api/v1/interviews/{id}/events`.
- Scale horizontally only with a shared database or by pinning a session to one instance, since
  storage is local SQLite.

## 9. Security and privacy controls

- **Rate limits** are per tenant: `RATE_LIMIT_PER_MINUTE` for all calls, `RATE_LIMIT_EXPENSIVE_PER_MINUTE` plus a `DAILY_EXPENSIVE_BUDGET` for anything that runs an LLM, speech-to-text or TTS. Limits return 429 with `Retry-After`. Repeated bad keys from one address are locked out for a minute. Limits live in process memory, so also rate-limit at your gateway when running several workers.
- **Documents** are validated (PDF/ZIP signature, page count, decompressed size), then parsed in a child process with a hard timeout.
- **Prompt injection:** candidate text is fenced in `<untrusted_...>` blocks, the model is told to treat it as data, and LLM scores are bounded to the deterministic baseline plus or minus `SCORE_CLAMP_DELTA` (10 when injection phrasing is detected). Suspicious resumes, JDs and answers raise an `integrity.possible_prompt_injection` event and `metrics.integrity_flags`. Treat model scores as advisory, never as the only hiring signal.
- **Erasure and retention:** pass your own user id as `external_ref` when creating an interview, then `GET /api/v1/interviews?external_ref=<id>` lists a student's interviews and `DELETE /api/v1/interviews/by-ref/<id>` erases all of them. `DELETE /api/v1/interviews/{id}` removes the interview, answers, report, events and any audio. Set `RETENTION_DAYS` to purge old interviews automatically. Audio is discarded after transcription unless `RETAIN_AUDIO=true`.
- **Encryption at rest is not done in the app.** On Supabase, data is encrypted at rest by the platform. With the SQLite fallback, use an encrypted volume. See `DEPLOY_FLY_SUPABASE.md` for the production setup.
- **Third-party AI processing:** resumes, JDs and answers are sent to whichever of DeepSeek, OpenRouter (which forwards to an upstream host), OpenAI and Anthropic is configured. Use `LLM_DISABLED_PROVIDERS=deepseek` to exclude a vendor, and set `REQUIRE_CONSENT=true` so each interview must be created with `"consent_to_ai_processing": true` (recorded in the event log). Your privacy policy must cover this processing.
