class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationAppError(AppError):
    status_code = 422
    code = "validation_error"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ProviderError(AppError):
    status_code = 502
    code = "provider_error"


class AllProvidersFailedError(ProviderError):
    code = "all_providers_failed"


class PolicyDeniedError(AppError):
    status_code = 403
    code = "policy_denied"


class SafetyViolationError(AppError):
    status_code = 500
    code = "safety_violation"


class SpeechUnavailableError(AppError):
    status_code = 503
    code = "speech_unavailable"
