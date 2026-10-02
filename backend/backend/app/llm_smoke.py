"""Send one tiny request to every distinct model setting in a profile, to check keys, model names and per-model options.

    python -m app.llm_smoke                    # the default (economy) profile
    python -m app.llm_smoke --profile premium

Providers without an API key (or switched off by LLM_DISABLED_PROVIDERS) are listed as skipped, not called. Each real
call costs a fraction of a cent. Exits with status 1 if any configured model fails.
"""

import argparse
import asyncio
import json
import sys
from typing import Any

from app.llm.providers.base import LLMMessage, ProviderCallError
from app.llm.router import ModelRouter
from app.llm.safety import bound_provider_call, redact_secrets

PROMPT = [
    LLMMessage(role="system", content="You reply with strict JSON only."),
    LLMMessage(role="user", content='Reply with exactly {"ok": true}'),
]


async def smoke(router: ModelRouter, profile: str | None = None) -> list[dict[str, Any]]:
    profile_name = profile or router.config.default_profile
    seen: set[tuple[str, str, str]] = set()
    results: list[dict[str, Any]] = []
    for tier in router.config._tiers_for(profile):  # noqa: SLF001 - a diagnostics tool reads the config directly
        for candidate in router.config.candidates(tier, profile):
            key = (candidate.provider, candidate.model, json.dumps(candidate.options, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            row = {"profile": profile_name, "tier": tier, "provider": candidate.provider, "model": candidate.model}
            if not router.provider_configured(candidate.provider):
                results.append({**row, "status": "skipped", "detail": "no API key, or disabled by policy"})
                continue
            provider = router._providers[candidate.provider]  # noqa: SLF001
            extra = {"options": candidate.options} if candidate.options else {}
            try:
                with bound_provider_call(task="answer_coaching", provider=candidate.provider, model=candidate.model):
                    response = await provider.complete(
                        PROMPT,
                        model=candidate.model,
                        temperature=router.settings.llm_temperature,
                        max_tokens=64,
                        **extra,
                    )
                results.append(
                    {
                        **row,
                        "status": "ok",
                        "detail": f"{response.input_tokens} in / {response.output_tokens} out: {response.text[:60]}",
                    }
                )
            except ProviderCallError as exc:
                results.append({**row, "status": "error", "detail": redact_secrets(exc.message)[:300]})
            except Exception as exc:  # noqa: BLE001
                results.append({**row, "status": "error", "detail": redact_secrets(f"{type(exc).__name__}: {exc}")[:300]})
    return results


def render(results: list[dict[str, Any]]) -> tuple[str, bool]:
    lines = []
    for r in results:
        lines.append(f"{r['status'].upper():8} {r['profile']:8} {r['tier']:9} {r['provider']}/{r['model']}  {r['detail']}")
    ok = not any(r["status"] == "error" for r in results)
    called = sum(1 for r in results if r["status"] != "skipped")
    lines.append("")
    lines.append(f"{called} model(s) called, {sum(1 for r in results if r['status'] == 'error')} failed, "
                 f"{sum(1 for r in results if r['status'] == 'skipped')} skipped")
    return "\n".join(lines), ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=None, help="model profile from models.yaml (default: the default profile)")
    args = parser.parse_args()
    from app.llm.router import get_router

    text, ok = render(asyncio.run(smoke(get_router(), args.profile)))
    print(text)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
