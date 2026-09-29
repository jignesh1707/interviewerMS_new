from typing import Any

import httpx

from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.llm.safety import is_approved_endpoint, redact_secrets, require_boundary_permit


def _classify(status_code: int, body: str) -> ProviderCallError:
    retryable = status_code in (408, 409, 425, 429) or status_code >= 500
    auth_error = status_code in (401, 403)
    if auth_error:
        retryable = False
    return ProviderCallError(
        f"provider returned HTTP {status_code}: {redact_secrets(body[:400])}",
        retryable=retryable,
        auth_error=auth_error,
        status_code=status_code,
    )


class OpenAICompatibleClient:
    def __init__(self, name: str, base_url: str, api_key: str, timeout: float) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout

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
    ) -> ProviderResponse:
        require_boundary_permit()
        if not self.configured:
            raise ProviderCallError(f"{self.name} is not configured or endpoint is not approved", retryable=False)

        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
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
            raise _classify(response.status_code, response.text)

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderCallError(f"{self.name} returned non-JSON body", retryable=False) from exc
        if not isinstance(data, dict):
            raise ProviderCallError(f"{self.name} returned a non-object payload", retryable=False)
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise ProviderCallError(f"{self.name} returned no choices", retryable=False)
        first = choices[0] if isinstance(choices[0], dict) else {}
        content = ""
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        if isinstance(message.get("content"), str):
            content = message["content"]
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        model_name = data.get("model") if isinstance(data.get("model"), str) else model
        return ProviderResponse(
            text=content.strip(),
            model=model_name,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw={},
        )
