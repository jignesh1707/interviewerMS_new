# Data handling and sub-processors

**Status: engineering draft for legal review.** This describes what the service does today, taken from the code. It is
not legal advice and it is not a certification. In particular, **it does not claim that no data reaches an AI
provider: it does, and this document says exactly what.** The privacy policy, consent text and any data processing
agreements must be written and approved by the people responsible for them.

## 1. What the service is

A practice tool for job interviews (STAR-style behavioural questions) used mainly by students on F-1/OPT in the USA.
The main app (resumetojob) sends a student's resume text and a job description; the student answers questions by voice
or text; the service returns scores and a report.

## 2. Data inventory

| Data | Where it comes from | Where it is stored | Sent to an AI provider? |
|---|---|---|---|
| Student reference (`external_ref`) | Main app (a hashed user id is recommended) | Postgres: `interviews`, `packs`, `pack_payments`; Redis keys for rate limits | No |
| Resume text | Main app | Postgres `interviews.resume_text` | **Yes, redacted** (see section 4), in the resume-summary request only |
| Job description text | Main app | Postgres `interviews.jd_text` | Only extracted skills and up to 8 requirement lines, not the whole text |
| Candidate name (optional) | Main app | Postgres `interviews.candidate_name` | **No.** It is used only to remove that name from other text |
| Spoken answers (audio) | Student's browser | **Not stored** by default (`RETAIN_AUDIO=false`); transcribed locally | **No.** Audio never leaves the service |
| Answer transcripts | Local speech-to-text, or typed | Postgres `answers.transcript` | **Yes, redacted**, for scoring and follow-up questions |
| Scores, analysis, report | Generated | Postgres `answers`, `reports` | Report inputs (question text, scores, short strengths and improvements) go to the final scoring and narrative requests |
| Questions | Generated | Postgres `interviews.questions` | They are the AI output |
| Purchases | Main app, after payment | Postgres `pack_payments` (student ref, plan, payment id, dates) and `packs` (minutes, expiry) | No |
| Audit events | Service | Postgres `events` (event types, routing info, matched prompt-injection phrases, the consent flag) | No |

Application logs carry interview ids, task names, event names and error messages. Resumes, transcripts and request
bodies are not written to them.

## 3. Data flow

```
Student's browser ──> main app backend ──(API key, private network)──> interview service (Fly.io)
                                                                         │ speech-to-text and text-to-speech run here
                                                                         ├─> Postgres (Supabase)        stored data
                                                                         ├─> Redis (Upstash)            rate-limit counters
                                                                         └─> AI provider(s) for the plan  redacted text only
```

The browser never talks to the service directly and never sees its API key.

## 4. What an AI provider receives, and what is removed first

Before a resume or an answer is sent, the service removes (best effort): names (the first-line name, and any name the
caller supplied), email addresses, phone numbers, links (LinkedIn, GitHub, personal sites), street addresses and ZIP
codes, ID-style numbers, and **lines about work authorization or immigration status** (OPT, H-1B, visa sponsorship,
citizenship and similar). Controlled by `REDACT_PII_FOR_LLM` (default on). Only the copy sent to the provider changes.

This is **minimization, not anonymization.** A resume still contains employers, schools, projects and dates, and a
student can say anything aloud. Do not describe the data as anonymous.

## 5. Sub-processors

| Provider | Role | Data it handles | Used by |
|---|---|---|---|
| Fly.io | Hosting the service (region of your choice) | Everything in section 2, in memory and in transit | All plans |
| Supabase | Postgres database (region of your choice) | Everything stored in section 2 | All plans |
| Upstash | Redis | Rate-limit counters, keyed by tenant and student reference; expire after a minute or two days | All plans |
| DeepSeek | AI model | Redacted resume text and answers | Economy plan only |
| OpenRouter | AI model gateway to the same open models | Redacted resume text and answers | Economy plan, as fallback |
| OpenAI | AI model | Redacted resume text and answers | Fallback on both plans |
| Anthropic | AI model | Redacted resume text and answers | Premium plan first; Economy only as a last fallback |

`GET /api/v1/plans` returns, for each plan, the providers that may process its data, computed from the configuration
and excluding any provider switched off with `LLM_DISABLED_PROVIDERS`. Use that to build the consent screen so it
cannot drift from what the service does. **Premium never reaches DeepSeek or OpenRouter**: the plan carries a provider
allow-list that the router enforces on every call, and the service refuses to start if the model configuration
disagrees with it.

DeepSeek is a China-based provider. Check its current published terms on where data is processed and stored before
relying on it, and make sure the consent text for the Economy plan names it. If that is not acceptable, it can be
removed from the Economy profile without code changes (see `MODEL_ROUTING.md`).

Provider data-retention and training terms differ by provider and by model, and change. Obtain each provider's current
terms and data processing addendum, and do not promise zero retention unless the provider has confirmed it for the
model in use.

## 6. Security controls (what exists in the code)

- API-key authentication per tenant; a tenant can read only its own interviews. The service will not start outside
  development with a default or short key.
- Postgres tables live in a dedicated schema with row-level security enabled and no policies, so Supabase's automatic
  REST API cannot read them.
- Per-tenant and per-student rate limits and daily budgets, shared across machines through Redis.
- Resume and job-description files are parsed in a separate child process with a hard timeout and size limits.
- Prompt-injection hardening: untrusted text is fenced in prompts, model scores are clamped to the deterministic
  baseline, and suspicious phrases are recorded as events.
- Webhooks are signed (HMAC-SHA256 with a timestamp for replay protection); callback URLs must be public HTTPS hosts.
- Calls to AI providers go only to an allow-list of HTTPS hosts. Secrets are redacted from logs and error messages.
- Server-side time limit, per-answer size caps, and purchases that are applied once per payment id.

Not provided: SOC 2, ISO 27001 or any other certification for this service itself. The hosting and AI vendors publish
their own reports, which can be referenced.

## 7. Retention and deletion

- **Audio:** discarded after transcription (`RETAIN_AUDIO=false`).
- **Interviews, answers, reports, events:** kept forever unless `RETENTION_DAYS` is set. **Set a value before going
  live.** A background sweep deletes older interviews hourly.
- **Erase one student:** `DELETE /api/v1/interviews/by-ref/{external_ref}` deletes their interviews, answers, reports,
  events and any stored audio. Supabase's daily backups can keep erased rows until they expire; say so in the privacy
  policy.
- **Purchases:** `packs` and `pack_payments` hold only the student reference, plan, minutes, payment ids and dates. They
  are deliberately **not** removed by the erase endpoint, because deleting them would let a student reset a balance or
  a refund window. Decide with counsel whether and when they are deleted.

## 8. What the main app must do

- Show consent text that names the providers for the plan being bought (from `GET /api/v1/plans`), and store who
  consented, when, and to which version of the text. Send `consent_to_ai_processing: true` when creating an interview;
  set `REQUIRE_CONSENT=true` on the service to make that mandatory.
- Update its privacy policy: the new processors, the purposes, the retention period, and how to request deletion.
- Call the erase endpoint when a user deletes their account.
- Keep the student reference stable (never rotate a hashing pepper) so balances and erasure keep matching.

## 9. Questions for legal and the business owners

1. Are the main app and this service operated by the same legal entity? If not, a data processing agreement is needed
   between them.
2. Signed data processing agreements, and current data-retention and training terms, from each AI provider in use.
3. Whether Economy may keep using a China-based provider, with what wording, or should move to US providers only.
4. The retention period for interviews and for purchase records.
5. State privacy laws that apply (for example California), rules on voice and biometric data (for example Illinois;
   audio here is used only for transcription and no voiceprint is created), and the position of students from other
   countries whose home-country rules may apply.
6. A breach-notification process and a contact for privacy requests.
7. Whether work-authorization information in resumes should be handled any more strictly than line removal.
