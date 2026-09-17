from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass
class LLMMessage:
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ProviderResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderCallError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        auth_error: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.auth_error = auth_error
        self.status_code = status_code
