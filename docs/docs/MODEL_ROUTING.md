# Model routing and cost control

The router lives in `backend/app/llm/router.py` and is configured by `backend/models.yaml`.

## Concepts

- **Tier**: an ordered list of `{provider, model}` candidates. Fallback order is DeepSeek, then OpenRouter, then OpenAI, then Anthropic.
- **Task**: a named unit of work (`question_generation`, `final_scoring`, ...).
- **Task map**: assigns each task to a tier, so simple work never reaches an expensive model.
- **Circuit breaker**: a provider that keeps failing is temporarily removed from the candidate list.
- **Fallback**: if a candidate fails, the router tries the next candidate, across providers.

## Configuration

`backend/models.yaml`:

```yaml
tiers:
  cheap:
    - provider: openai
      model: gpt-4o-mini
      input_price: 0.15      # USD per 1M input tokens
      output_price: 0.60
      priority: 0
    - provider: deepseek
      model: deepseek-chat
      input_price: 0.27
      output_price: 1.10
      priority: 1
    - provider: anthropic
      model: claude-3-5-haiku-latest
      input_price: 0.80
      output_price: 4.00
      priority: 2

  standard: [ ... ]
  premium:  [ ... ]

tasks:
  followup_generation: cheap
  question_generation: standard
  answer_analysis: standard
  final_scoring: premium

default_tier: standard
```

Rules:

- Candidate order inside a tier is the fallback order. Put DeepSeek first, then OpenRouter, then OpenAI, then Anthropic.
- OpenRouter is called at `https://openrouter.ai/api/v1` with the model id `deepseek/deepseek-v4.1-flash`. By default requests carry `provider.data_collection = deny` (`OPENROUTER_DATA_COLLECTION`), so OpenRouter only routes to upstream hosts that do not retain or train on prompts. If no such host is available the call fails and routing moves on.
- Prices are only used for the cost estimate reported in `/api/v1/models` and `routing_trace`; they
  are not used for routing decisions. Update them to match current provider pricing.
- Set `priority` on a candidate to override YAML order (lower is tried first).
- A provider with no API key in `.env` is skipped, so you can run with a subset of platforms.
- `input_price` / `output_price` default to `0`, which simply yields `$0` estimates.

## Overriding a tier per call

```python
result = await router.complete("question_generation", messages, tier="premium")
```

This is useful for forcing a higher tier on an important candidate without changing global config.

## Profiles: a different provider list per plan

`models.yaml` has a default profile (the top-level `tiers`, named `economy`) and extra profiles under `profiles:`, each
with its own `cheap`, `standard` and `premium` lists. `plans.yaml` picks one per plan with `llm_profile`. The shipped
setup:

- `economy`: DeepSeek, then OpenRouter, OpenAI, Anthropic.
- `premium`: Anthropic (Haiku 4.5 for light work, Sonnet 5.5 for the rest), then OpenAI. No DeepSeek.

Rules:

- **Fallbacks never leave a profile.** A request for the `premium` profile can only ever try that profile's candidates.
- **An unknown profile is an error**, never a silent fallback to `economy`.
- **A plan's `llm_allowed_providers`** is passed with every AI call (`authorized_providers`) and enforced by the router
  regardless of `models.yaml`. At startup the service refuses to start if a plan names a profile that does not exist,
  or if its profile lists a provider the plan forbids.
- **Replacing a provider** (for example removing DeepSeek from Economy) is an edit to `models.yaml` plus a restart. If
  you change which companies process student data, update the privacy policy and consent text in the main app.
  `GET /api/v1/plans` shows the providers each plan may use right now.

Per-model `options` (optional) adjust the request. The Anthropic adapter understands `omit_temperature` (newer Claude
models reject a non-default temperature) and `body`, extra request fields such as `thinking: {type: between_tools}`
(turns thinking off, which is on by default and billed as output) and `output_config: {effort: low}`. They can never
replace `model`, `messages`, `max_tokens` or `system`. Haiku 4.5 does not accept `effort`, so it has no options.

Check a profile against the live APIs (one tiny request per model; skips providers without a key; exit status 1 if a
model fails):

```bash
python -m app.llm_smoke --profile premium
python -m app.llm_smoke                    # the default (economy) profile
```

## Failure handling

1. The router builds a plan for the tier, skipping unconfigured providers and providers whose
   circuit is open.
2. For each candidate it attempts up to `LLM_MAX_ATTEMPTS_PER_PROVIDER` calls.
3. Retryable failures (timeouts, 429, 5xx) allow a retry; auth errors (401/403) do not.
4. On failure the provider's cooldown grows with consecutive failures (capped at 5x) and auth errors
   get a minimum 15-minute cooldown.
5. If every candidate fails, `AllProvidersFailedError` is raised with the full attempt history.

The interview engine catches that error and degrades instead of failing the request:

- Question generation failure fails the interview creation (there is nothing to ask without it).
- Per-answer analysis failure falls back to Python heuristic scores.
- Final scoring failure produces a deterministic heuristic scorecard.

## Observability

`GET /api/v1/models` returns:

```json
{
  "providers": {
    "openai": {"configured": true, "available": true, "consecutive_failures": 0, "disabled_for_seconds": 0.0, "last_error": ""}
  },
  "tiers": {"cheap": [{"provider": "openai", "model": "gpt-4o-mini"}]},
  "tasks": {"final_scoring": "premium"},
  "usage": {"calls": 12, "input_tokens": 18432, "output_tokens": 5120, "estimated_cost_usd": 0.0105, "by_model": {}}
}
```

Each answer and report also embeds a `routing_trace` so you can audit which model actually served a
step and whether failover happened.

## Adding a provider

See `docs/security/EXTERNAL_MODEL_SAFETY_BOUNDARY.md`. Every adapter must call
`require_boundary_permit()` and must not define its own security policy.

1. Implement a client with `configured` and `async complete(messages, model, temperature, max_tokens)`
   in `backend/app/llm/providers/`.
2. Call `require_boundary_permit()` at the start of `complete`.
3. Register it in `ModelRouter._build_providers` using an approved Settings key/URL.
4. Add the provider host to the shared HTTPS allowlist in `app/llm/safety.py`.
5. Add its API key and base URL to `config.py` and `.env.example`.
6. Add candidates for it in `models.yaml`.
7. Do not add provider-specific security branches.

## Choosing a tier for a new task

- Deterministic or near-deterministic output, short responses → `cheap`.
- Structured generation needing judgement → `standard`.
- Scoring, evaluation or narrative synthesis where mistakes are costly → `premium`.

If a task can be done reliably in Python, do it in Python and skip the router entirely. That is the
cheapest option and is already used for parsing, metrics and fallback scoring.
