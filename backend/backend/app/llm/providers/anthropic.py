from typing import Any

import httpx

from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.llm.safety import is_approved_endpoint, redact_secrets, require_boundary_permit


RESERVED_FIELDS = frozenset({"model", "messages", "max_tokens", "system", "stream"})


class AnthropicClient:
    def __init__(self, name: str, base_url: str, api_key: str, timeout: float, version: str = "2023-06-01") -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.version = version

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def configured(self) -> bool:
        return bool(self._api_key) and is_approved_endpoint(self.base_url, self.name)

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        temperature: float,
        max_tokens: int,
        options: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        require_boundary_permit()
        if not self.configured:
            raise ProviderCallError(f"{self.name} is not configured or endpoint is not approved", retryable=False)

        url = f"{self.base_url}/v1/messages"
        system_parts = [message.content for message in messages if message.role == "system"]
        conversation = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in ("user", "assistant")
        ]
        if not conversation:
            raise ProviderCallError("anthropic requires at least one user message", retryable=False)

        options = options or {}
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": conversation,
        }
        # Newer Claude models reject a non-default temperature, so models.yaml can switch it off per model.
        if not options.get("omit_temperature"):
            payload["temperature"] = temperature
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        # Extra request fields from models.yaml (for example thinking and effort settings). They can never replace
        # the fields the router controls.
        for key, value in (options.get("body") or {}).items():
            if key not in RESERVED_FIELDS:
                payload[key] = value

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise ProviderCallError(f"{self.name} request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderCallError(f"{self.name} transport error: {redact_secrets(str(exc))}") from exc

        if response.status_code >= 400:
            retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
            raise ProviderCallError(
                f"{self.name} returned HTTP {response.status_code}: {redact_secrets(response.text[:400])}",
                retryable=retryable,
                auth_error=response.status_code in (401, 403),
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderCallError(f"{self.name} returned non-JSON body", retryable=False) from exc
        if not isinstance(data, dict):
            raise ProviderCallError(f"{self.name} returned a non-object payload", retryable=False)
        blocks = data.get("content") or []
        text = ""
        if isinstance(blocks, list):
            text = "".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        model_name = data.get("model") if isinstance(data.get("model"), str) else model
        return ProviderResponse(
            text=text.strip(),
            model=model_name,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw={},
        )
