import logging
import sys

from app.config import get_settings

_CONFIGURED = False
_SECRET_FILTER: "_SecretFilter | None" = None
_ORIGINAL_FACTORY = logging.getLogRecordFactory()


class _SecretFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._values: tuple[str, ...] = ()

    def set_values(self, values: tuple[str, ...] | list[str]) -> None:
        self._values = tuple(item for item in values if item and len(item) >= 8)

    def redact_record(self, record: logging.LogRecord) -> logging.LogRecord:
        from app.llm.safety import redact_secrets

        record.msg = redact_secrets(str(record.msg), self._values)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_secrets(str(value), self._values) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_secrets(str(arg), self._values) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return record

    def filter(self, record: logging.LogRecord) -> bool:
        self.redact_record(record)
        return True


def _redacting_factory(*args, **kwargs):
    record = _ORIGINAL_FACTORY(*args, **kwargs)
    if _SECRET_FILTER is not None:
        _SECRET_FILTER.redact_record(record)
    return record


def install_secret_filter(values: tuple[str, ...] | list[str] | None = None) -> None:
    global _SECRET_FILTER
    configure_logging()
    if _SECRET_FILTER is None:
        _SECRET_FILTER = _SecretFilter()
        logging.setLogRecordFactory(_redacting_factory)
        root = logging.getLogger()
        root.addFilter(_SECRET_FILTER)
        for handler in root.handlers:
            handler.addFilter(_SECRET_FILTER)
    if values:
        existing = list(_SECRET_FILTER._values)
        for item in values:
            if item and item not in existing:
                existing.append(item)
        _SECRET_FILTER.set_values(existing)


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True
    install_secret_filter(
        (
            settings.openai_api_key,
            settings.deepseek_api_key,
            settings.anthropic_api_key,
            settings.webhook_secret,
            *settings.api_key_set,
        )
    )


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
