# Prompt for the resumetojob session: integrate the Interviewer service

Paste everything below the line into a Claude session whose working directory is the **resumetojob repo**
(`projects/resumeto/`). If that session can also read the Interviewer repo, point it at `docs/docs/INTEGRATION.md`
and `docs/docs/DATA_HANDLING.md` there; otherwise attach those two files. The contract is repeated below so the
session can start without them.

---

## Your task

Integrate resumetojob with the **Interviewer service**, a separate private microservice that runs mock voice
interviews for students. You work in the resumetojob repo. You do not change the Interviewer service.

**Work in phases and stop after Phase 0.** Do not write code until I have approved your Phase 0 plan. After that,
one phase per branch and per pull request. Open pull requests, never merge them, never deploy, never touch production
data or production secrets, and use Stripe **test mode** only.

If something in this prompt contradicts what you find in the repo, say so in your plan instead of guessing which is
right. If a fact you need is not in the repo and not in this prompt, list it as a question for me.

## What the Interviewer service is (facts, already built and tested)

- A private Fly app in the same Fly org as `resumetojob-api`. Staging: `http://interviewer-ms-staging.flycast`.
  Production (not created yet): `http://interviewer-ms.flycast`. Plain http over Fly's private network. Not reachable
  from browsers or the internet.
- Auth: send `X-API-Key: <key>` on every request. The key is a secret held by resumetojob's **backend** only. It must
  never reach a browser, a log line or an error message. Env names to use: `INTERVIEWER_BASE_URL`,
  `INTERVIEWER_API_KEY`, `INTERVIEWER_WEBHOOK_SECRET`, `INTERVIEWER_REF_PEPPER`.
- The service owns minutes, expiry and the refund rule. resumetojob owns payments, identity and the screens.
- Student identity: send `external_ref` = a hash of the Clerk `sub`, for example
  `hex(HMAC_SHA256(INTERVIEWER_REF_PEPPER, clerk_sub))`. **The pepper must never change**, or balances and erasure stop
  matching. Document that in the code next to the helper. This repo already has a similar pattern in
  `core/tailoring_client.py` (`user_id_hash`); reuse its style and, if sensible, its helper.

### Plans (from `GET /api/v1/plans`; read it at runtime, do not hard-code)

| Plan | Pack | Notes |
|---|---|---|
| `economy` | 150 minutes, 30 days, lengths 15/20/25 | price $10 |
| `premium` | 250 minutes, 30 days, lengths 15/20/25 | price $20 |
| `included_weekly` | 30 minutes, 14 days, length 15 only (two interviews) | **not sold**: granted with the weekly subscription |
| `included_monthly` | 75 minutes, 35 days, length 15 only (five interviews) | **not sold**: granted with the monthly subscription |
| `included_quarterly` | 270 minutes, 100 days, length 15 only (eighteen interviews) | **not sold**: granted with the quarterly subscription |

Add-ons the student buys on top (Stripe one-time payments), activated with a **`sku`**, not a plan. Read the list from
`GET /plans` (`skus`), do not hard-code the numbers:

| Add-on | sku(s) to activate | Result |
|---|---|---|
| $5 | `addon_5` | 150 Economy minutes (10 interviews of 15) |
| $10 | `addon_10_economy` **or** `addon_10_premium` (student chooses at checkout) | 300 Economy minutes (20 x 15) **or** 75 Premium minutes (5 x 15, Anthropic) |
| $20 | `addon_20_economy` **and** `addon_20_premium`, from ONE payment | 650 Economy minutes (any length) **and** 250 Premium minutes (10 x 25) |

The $20 add-on needs **two activate calls with different `payment_id`s** (for example `<payment_intent>:economy` and
`<payment_intent>:premium`). To refund it, check `refund_eligible` on both wallets first and then call `/packs/revoke`
twice (once per wallet, each with its own payment id); never refund when only one wallet is eligible.

- Prices live in Stripe and in resumetojob, **not** in the service. The service only knows plan names.
- Buying again while a pack is active **stacks** (minutes added, expiry pushed back). Buying after expiry or
  exhaustion starts a fresh pack. Trial users get **no** minutes: only call activate for a real payment.
- Per `CLAUDE.md` section 11 of this repo, Voice Interview is a **separate paid add-on, never bundled into the
  resume/cover-letter plan counters**. Keep the counters untouched.
- Refund rule enforced by the service: a purchase is refundable only while at most 1 interview has been started
  **or** at most 15 minutes were used.

### Endpoints you will call (all under `/api/v1`, JSON unless noted)

| Purpose | Call |
|---|---|
| Plan catalog + which AI companies see data | `GET /plans` |
| Student paid | `POST /packs/activate` `{external_ref, plan OR sku, payment_id, purchased_at}` (send exactly one of `plan`/`sku`; idempotent on `payment_id`; replay returns `applied:false`) |
| Balance / refund eligibility | `GET /packs/{external_ref}` |
| Refund | `POST /packs/revoke` `{external_ref, plan, payment_id}` (409 `refund_not_allowed` = do NOT refund) |
| Subscription ended | `POST /packs/expire` `{external_ref, plan}` (ends the pack now, minutes gone, not a refund, idempotent, `expired:false` if there was no pack) |
| Start | `POST /interviews` `{role, candidate_name, resume_text, jd_text, external_ref, plan, consent_to_ai_processing: true, callback_url, metadata, config:{duration_minutes}}` → 201 with `interview.id`, `interview.deadline_at`, `questions[]` |
| Read a question aloud (optional) | `POST /speech/synthesize` form field `text` → `audio/wav` |
| Spoken answer | `POST /interviews/{id}/answers/audio` multipart `question_index`, `audio`, `duration_seconds` |
| Typed answer | `POST /interviews/{id}/answers` `{question_index, transcript, duration_seconds}` |
| Finish | `POST /interviews/{id}/finish` → **202** `{status:"processing", report:null}` (or 200 with the report if already done) |
| Report | `GET /interviews/{id}/report` → `{status: processing\|completed\|failed, report, error}`; poll every 2.5 s |
| Status | `GET /interviews/{id}/status` |
| Erase a student | `DELETE /interviews/by-ref/{external_ref}` (answers, reports, audio; **not** the pack balance) |

### Errors your client must handle on purpose

- **503 with a `Retry-After` header** (speech busy, for example "speech recognition is busy, retry shortly"): wait
  that many seconds and retry, up to about 6 times, then show a friendly "try again". Applies to answers and to
  `/speech/synthesize`.
- **402 `quota_exceeded`** with `details.reason` in `no_active_pack | pack_expired | no_minutes_left |
  insufficient_minutes` (plus `remaining_minutes`, `expires_at`, `days_remaining`): show the right message / buy-pack
  screen.
- **409 `time_limit_reached`**: answers are closed; call `finish` so the student still gets the report.
- **429**: back off. **502 / 5xx on create or answer**: show a retry; never double-charge minutes (the service
  refunds the booking if creation fails).
- **Timeouts**: the service makes AI calls that usually take 4–30 s and occasionally more. Create can take up to
  ~90 s and an answer up to ~60 s in bad cases. Use **at least 120 s** for create / answer calls and show a
  "thinking…" state. Never use a 60 s timeout.
- `finish` must accept **both** 200 and 202. A second `finish` while processing is safe (no second build).

### Webhooks from the service to resumetojob

- Events: `interview.created`, `interview.completed`. Payload:
  `{"event": "...", "sent_at": 1750000000.0, "data": {"interview_id": "...", "status": "completed", "overall_score": 76, "readiness_level": "..."}}`.
  No personal data in it.
- Headers: `X-Interview-Event`, `X-Interview-Delivery` (unique per delivery; **dedupe on it**, retries reuse it),
  `X-Interview-Timestamp`, `X-Interview-Signature-V2` = hex HMAC-SHA256 of `"<timestamp>.<raw body>"` with the shared
  secret. **Verify V2 on the raw body, reject timestamps older than 5 minutes, constant-time compare, and ignore the
  legacy `X-Interview-Signature`.** Python reference:

  ```python
  import hashlib, hmac, time
  def verify(raw_body: bytes, timestamp: str, signature_v2: str, secret: str, tolerance: int = 300) -> bool:
      if abs(time.time() - int(timestamp)) > tolerance:
          return False
      expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, signature_v2)
  ```
- Delivery is **at least once** with retries after 30 s, 2 min, 10 min, 1 h, 6 h, 12 h. Answer **2xx fast**; 5xx and
  429 are retried, other 4xx are not. The `callback_url` must be `https`, must be on the service's allow-list and
  must resolve to a public address, so use resumetojob's **public** host (`https://resumetojob-api.fly.dev/...`),
  not the private network. Webhooks can be lost, so also **poll** for any interview you still show as processing.
- Stripe webhook handling in this repo is the pattern for raw-body + HMAC receivers (`POST /billing/webhook`).

## Decisions already made (do not re-open; flag if the repo makes one impossible)

0. **Five interviews are included with the monthly subscription ($29.99).** Plan `included_monthly` (75 minutes, 15-minute
   interviews). Grant it on every **paid** subscription invoice (first payment and renewals; never during the trial, on
   $0 invoices or on proration invoices from plan changes) with `payment_id` = the invoice id. **Unused interviews roll
   over while the student keeps subscribing. When the subscription ends (cancellation effective, or unpaid and
   cancelled) call `POST /packs/expire`: whatever is left is gone, and there is no refund.** Handle Stripe events in
   order (do not activate for an invoice paid before you expired the pack). If the subscription is refunded, call
   `/packs/revoke` with the invoice id first (it always succeeds for this plan). Show the student "N interviews left"
   as `floor(minutes_remaining / 15)` and spend the included wallet before any bought `economy` pack. This changes
   `CLAUDE.md` section 11 (voice interview "never bundled"): update that sentence in the repo as part of Phase 1.
   **Weekly** subscribers get `included_weekly` (2 interviews) and **quarterly** subscribers get `included_quarterly`
   (18 interviews), under exactly the same rules. When a student switches billing period, expire the old plan when its
   subscription ends and grant the new one on its first paid invoice.
   **Add-ons never expire with the subscription**: a student keeps a bought $5/$10/$20 wallet for its 30 days. Show
   every wallet (included and bought) and let the app spend the included one first; the student can also choose.

1. Two plans for sale, Economy $10 and Premium $20, 30-day packs, separate from existing plans. Stacking and refund rule are
   in the service.
2. Interview screens live **inside the resumetojob web app** (Vercel, `www.resu2job.us`) and talk only to
   resumetojob's own API, which proxies to the service. No iframe, no browser call to the service.
3. Audience: F-1/OPT students in the USA. Consent before any AI processing is mandatory: the service refuses create
   unless `consent_to_ai_processing: true`.
4. The mic: use `MediaRecorder` in the browser; nothing in this repo's CSP / Permissions-Policy blocks it (verify).
   If server read-aloud (`/speech/synthesize`) fails or is busy, fall back to the browser's own speech synthesis.

## Open decisions: propose a recommendation for each in your Phase 0 plan

1. **Pack purchase model in Stripe.** The service models "a pack that expires and stacks", which fits **one-time
   Checkout payments (`mode=payment`) per pack**, not an auto-renewing subscription. Recommend one, and say what
   `payment_id` you would send. Use the Stripe **PaymentIntent id** (stable across event re-deliveries and the same id
   you need later to revoke on a refund), or the invoice id for a recurring model.
2. **Refunds.** The repo does not handle `charge.refunded`, disputes or `invoice.paid` today and has no event-id
   dedupe. Design: (a) an admin/support action that asks `GET /packs/{ref}` (`refund_eligible`), calls
   `POST /packs/revoke`, and only on success issues the Stripe refund; (b) a `charge.refunded` handler as a safety
   net that calls revoke and, on 409, logs and alerts (a refund done outside the rule, for example from the Stripe
   dashboard). Disputes cannot be refused; they should create an alert with the interview usage as evidence.
3. **Store a copy of the report or fetch it on demand?** Storing is simpler for screens but duplicates sensitive
   text; the service has a retention setting (not decided yet, so fetched reports may disappear later). Recommend a
   minimum: store `interview_id`, plan, minutes booked, status, score and readiness; fetch the full report from the
   service on demand, and tell me what breaks when retention deletes it.
4. **Consent record.** Store who consented, when, the exact consent text version, the plan and the list of AI
   companies shown (from `GET /plans` → `llm_providers`). The consent text must always match the service's real
   provider list.
5. **Account deletion.** `delete_user()` in `core/users.py` has no caller yet. Hook the interview erase
   (`DELETE /interviews/by-ref/...`) before the users row is deleted, and decide what happens to an unused paid pack
   (the erase does not touch balances).
6. **Database.** Supabase Postgres. Propose the migration style the repo already uses and the table(s), for example
   a `voice_interviews` table (user id, service interview id, plan, status, minutes, score, timestamps) and a
   `voice_interview_consents` table, plus a payments/processed-events table for Stripe event dedupe.

## Phases

**Phase 0: explore and plan only (no code).** Read the repo and report:
- how Stripe checkout, the webhook, plans and quota counters work today (files and functions), and exactly where a
  new add-on product fits without touching the existing counters;
- how `core/tailoring_client.py` calls its service, how errors, timeouts and retries are handled, and how its tests
  work; the test, lint and migration commands;
- how auth works for the web app's calls (Clerk JWT) and where a new route group would live;
- the frontend stack, where the interview screens would go, and how API calls and loading states are done today;
- your recommendation for each open decision above, a table of new env vars and secrets (names only, never values),
  a file-by-file change list, risks, and the order of phases below with a size estimate for each.
End with a numbered list of questions for me. Then stop and wait.

**Phase 1: service client and money path (backend).** A small typed client for the service (timeouts, `Retry-After`
handling, error mapping, never logs the key or student text); `external_ref` helper; Stripe add-on product handling
and the verified-webhook → `POST /packs/activate` path with idempotency and Stripe event-id dedupe; a balance
endpoint for the web app. Unit tests plus a fake-service contract test that replays the error cases above.

**Phase 2: interview proxy and report path (backend).** Authenticated routes: create (with consent check and plan
choice), answer (text and audio), finish, status/report; the signed webhook receiver with dedupe; a reconciliation
job or on-read poll for interviews stuck in processing; limits so one user cannot start unbounded interviews.

**Phase 3: refunds, erase, consent (backend).** The refund flow and `charge.refunded` safety net, account-deletion
hook, consent storage.

**Phase 4: screens (frontend).** Plan/buy screen with the balance and days left; consent screen built from
`GET /plans`; interview screen (question, record, "thinking…" state, retry on 503, countdown from
`seconds_remaining - grace_seconds`, read-aloud with browser-voice fallback); processing and report screens. Follow the
repo's existing design system. Handle mic-permission denial and a lost connection gracefully.

**Phase 5: staging end to end.** Point a staging deployment at `http://interviewer-ms-staging.flycast`, run through:
buy (Stripe test) → balance → start → 2 spoken answers → finish → report by webhook and by polling → refund → erase.
Write the checklist you ran and the result of each step.

## Rules for every phase

- Never log, return or store: the service API key, the webhook secret, the pepper, raw resumes, transcripts or audio
  (beyond what a screen needs). Error messages to users must not include provider names or raw upstream bodies.
- Tests first for the money path (activate, replay, revoke, 409 refund_not_allowed, webhook signature, replayed
  webhook, bad timestamp). Run the repo's full test suite and linters before each PR and paste the results.
- Keep PRs small and reviewable. Each PR description: what and why, how it was tested, what is not covered, the new
  env vars (names only). Do not merge.
- Report progress with evidence (command output), not claims. If something fails or you skip it, say so plainly.
