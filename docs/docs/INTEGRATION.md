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

When done, call `POST /api/v1/interviews/{id}/finish` to receive the report, or rely on the
`interview.completed` webhook.

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

## 4b. Plans, student quota and interview length

Off by default. Set `PLANS_ENABLED=true` and edit `backend/plans.yaml` (no code change, restart to apply).

```yaml
profiles:                       # what each interview length contains
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
  25: {question_count: 12, max_followups: 3}
plans:
  standard:
    included_minutes: 150       # 10 x 15, 8 x 20 or 6 x 25 minute interviews, or any mix
    period: monthly             # "none" = one lifetime balance (a one-time pack)
    durations: [15, 20, 25]
    default_duration: 15
```

When enabled:

- **Student id is required.** Send `external_ref` on create. The quota is kept per tenant and `external_ref`.
- **Choose a length** with `config.duration_minutes` (default: the plan's default). A length the plan does not allow returns 422. The length sets `question_count` and `max_followups`; values you send for those are replaced.
- **Booking debits the full length** when the interview is created. If the student does not have enough minutes the call returns **402** with `code: quota_exceeded` and `details.remaining_minutes`. If creation fails (for example the question model is down) the minutes are given back.
- **Server-side deadline.** The create response has `interview.duration_minutes` and `interview.deadline_at` (length plus `grace_seconds`, counted from when the questions are ready). After it, answers return **409** `time_limit_reached`; `finish` still works so the student gets the report. Audio is refused before any transcription work is spent. The status (create, `GET .../status`) also carries `grace_seconds` and `seconds_remaining` (to `deadline_at`, measured by the server, so a countdown does not depend on the student's clock). The nominal end to show the student is `seconds_remaining - grace_seconds`; the demo UI's countdown (`frontend/frontend/src/countdown.ts`) does exactly that, warning at 5 minutes and 1 minute, allowing the grace period, then disabling answer input at the deadline.
- **Per-answer caps** (always on): `MAX_ANSWER_SECONDS` (180) and `MAX_TRANSCRIPT_CHARS` (4000). Longer answers return 422 and are not stored.
- `POST /api/v1/interviews` and `/upload` also accept an optional `plan` (a name from `plans.yaml`; default plan otherwise).

Balance and top-ups:

```bash
curl -s  http://localhost:8080/api/v1/quotas/STUDENT_ID -H "X-API-Key: $KEY"
# {"plan":"standard","period":"monthly","period_key":"2026-10","included_minutes":150,
#  "bonus_minutes":0,"used_minutes":45,"remaining_minutes":105,"allowed_durations":[15,20,25]}

curl -sX POST http://localhost:8080/api/v1/quotas/STUDENT_ID/grant -H "X-API-Key: $KEY"   -H "Content-Type: application/json" -d '{"minutes": 30}'     # add minutes for the current period
```

**Per-student rate limits** (independent of `PLANS_ENABLED`): once an interview has an `external_ref`, its LLM and speech-backed calls (create, answer, audio answer, finish) also count against that student: `STUDENT_RATE_LIMIT_PER_MINUTE` (30) and `STUDENT_DAILY_BUDGET` (200 per UTC day), `0` to turn either off. They are kept per tenant and student, shared across machines through Redis, and return 429 like the tenant limits. One student cannot use up the tenant's shared limits on their own, but the tenant limits still apply on top. The standalone `/speech/transcribe` and `/speech/synthesize` calls carry no student id, so only the tenant limits cover them.

Billing stays in your main app: take the payment there, then call `grant` for any top-up. Quota rows are keyed by `external_ref` and are not removed by the erase-by-user endpoint, so erasing a student does not reset their balance.

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
