from typing import Any

import httpx

from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse


class AnthropicClient:
    def __init__(self, name: str, base_url: str, api_key: str, timeout: float, version: str = "2023-06-01") -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.version = version

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        url = f"{self.base_url}/v1/messages"
        system_parts = [message.content for message in messages if message.role == "system"]
        conversation = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in ("user", "assistant")
        ]
        if not conversation:
            raise ProviderCallError("anthropic requires at least one user message", retryable=False)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": conversation,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise ProviderCallError(f"{self.name} request timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ProviderCallError(f"{self.name} transport error: {exc}") from exc

        if response.status_code >= 400:
            retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
            raise ProviderCallError(
                f"{self.name} returned HTTP {response.status_code}: {response.text[:400]}",
                retryable=retryable,
                auth_error=response.status_code in (401, 403),
                status_code=response.status_code,
            )

        data = response.json()
        blocks = data.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        usage = data.get("usage") or {}
        return ProviderResponse(
            text=text.strip(),
            model=data.get("model", model),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )
