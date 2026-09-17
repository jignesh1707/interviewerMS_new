from typing import Any

import httpx

from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse


def _classify(status_code: int, body: str) -> ProviderCallError:
    retryable = status_code in (408, 409, 425, 429) or status_code >= 500
    auth_error = status_code in (401, 403)
    if auth_error:
        retryable = False
    return ProviderCallError(
        f"provider returned HTTP {status_code}: {body[:400]}",
        retryable=retryable,
        auth_error=auth_error,
        status_code=status_code,
    )


class OpenAICompatibleClient:
    def __init__(self, name: str, base_url: str, api_key: str, timeout: float) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

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
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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
            raise _classify(response.status_code, response.text)

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderCallError(f"{self.name} returned no choices")
        content = choices[0].get("message", {}).get("content") or ""
        usage = data.get("usage") or {}
        return ProviderResponse(
            text=content.strip(),
            model=data.get("model", model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )
