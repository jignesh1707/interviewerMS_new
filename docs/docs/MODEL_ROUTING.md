# Model routing and cost control

The router lives in `backend/app/llm/router.py` and is configured by `backend/models.yaml`.

## Concepts

- **Tier**: an ordered list of `{provider, model}` candidates from cheapest to strongest.
- **Task**: a named unit of work (`question_generation`, `final_scoring`, ...).
- **Task map**: assigns each task to a tier, so simple work never reaches an expensive model.
- **Circuit breaker**: a provider that keeps failing is temporarily removed from the candidate list.
- **Fallback**: if a candidate fails, the router tries the next candidate, across providers.

## Configuration

`backend/models.yaml`:

```yaml
tiers:
  cheap:
    - provider: deepseek
      model: deepseek-chat
      input_price: 0.27      # USD per 1M input tokens
      output_price: 1.10
    - provider: openai
      model: gpt-4o-mini
      input_price: 0.15
      output_price: 0.60

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

- Candidate order inside a tier is the fallback order. Put the cheapest viable option first.
- Prices are only used for the cost estimate reported in `/api/v1/models` and `routing_trace`; they
  are not used for routing decisions. Update them to match current provider pricing.
- A provider with no API key in `.env` is skipped, so you can run with a subset of platforms.
- `input_price` / `output_price` default to `0`, which simply yields `$0` estimates.

## Overriding a tier per call

```python
result = await router.complete("question_generation", messages, tier="premium")
```

This is useful for forcing a higher tier on an important candidate without changing global config.

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
    "deepseek": {"configured": true, "available": true, "consecutive_failures": 0, "disabled_for_seconds": 0.0, "last_error": ""}
  },
  "tiers": {"cheap": [{"provider": "deepseek", "model": "deepseek-chat"}]},
  "tasks": {"final_scoring": "premium"},
  "usage": {"calls": 12, "input_tokens": 18432, "output_tokens": 5120, "estimated_cost_usd": 0.0105, "by_model": {}}
}
```

Each answer and report also embeds a `routing_trace` so you can audit which model actually served a
step and whether failover happened.

## Adding a provider

1. Implement a client with `configured` and `async complete(messages, model, temperature, max_tokens)`
   in `backend/app/llm/providers/`.
2. Register it in `ModelRouter._build_providers`.
3. Add its API key and base URL to `config.py` and `.env.example`.
4. Add candidates for it in `models.yaml`.

## Choosing a tier for a new task

- Deterministic or near-deterministic output, short responses → `cheap`.
- Structured generation needing judgement → `standard`.
- Scoring, evaluation or narrative synthesis where mistakes are costly → `premium`.

If a task can be done reliably in Python, do it in Python and skip the router entirely. That is the
cheapest option and is already used for parsing, metrics and fallback scoring.
