# Integrating with the main app

The service is stateless from the caller's perspective: the main app creates an interview, submits
answers, and receives the report. Everything is persisted server-side in SQLite.

## 1. Authentication

Send the API key on every `/api/v1` request (except `/health` and `/ready`):

```
X-API-Key: <key>
```

Configure accepted keys on the service with `API_KEYS=key-one,key-two`.

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
    "final_scoring": {"provider": "deepseek", "model": "deepseek-reasoner", "tier": "premium", "fallbacks": 0}
  }
}
```

`routing_trace` tells you which provider and tier served each step, and how many failovers happened.

## 5. Webhooks

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
- `X-Interview-Signature` — HMAC-SHA256 of the raw request body using `WEBHOOK_SECRET`

Python verification:

```python
import hashlib, hmac

def verify(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Node verification:

```javascript
const crypto = require('crypto')

function verify(rawBody, signature, secret) {
  const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex')
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature))
}
```

Unset `WEBHOOK_SECRET` to skip signing (not recommended for production).

## 6. Polling alternative

If webhooks are not possible, poll:

- `GET /api/v1/interviews/{id}/status` — `status` moves `created` → `questions_ready` →
  `in_progress` → `processing` → `completed`.
- `GET /api/v1/interviews/{id}/report` — returns `report: null` until finished.

## 7. Embedding the UI

The demo frontend is a plain Vite app. To embed it:

1. Build it with `npm run build` in `frontend`.
2. Serve `frontend/dist` from the main app and proxy `/api` to this service.
3. Pass the API key into the UI (the demo stores it in `localStorage`; for production inject it from
   your authenticated session instead of asking the user).

The dev server already allows `*.monkeycode-ai.live` hosts and proxies `/api`, so it works behind the
platform preview.

## 8. Operational notes

- Audio is stored under `STORAGE_DIR/audio/{interview_id}`. Clean it up on your retention schedule.
- Reports and transcripts live in SQLite at `DATABASE_PATH`. Back up that file.
- Degraded model calls are recorded as `interview.llm_degraded` events, visible at
  `GET /api/v1/interviews/{id}/events`.
- Scale horizontally only with a shared database or by pinning a session to one instance, since
  storage is local SQLite.
