"""structlog configuration: JSON renderer, correlation id, secret masking."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import merge_contextvars

from utils.security import mask_sensitive, mask_text


def _collect_secrets(settings: Any) -> list[str]:
    candidates = []
    for attr in (
        "client_secret",
        "client_id",
        "discord_webhook_url",
        "alerting_webhook_url",
        "database_url",
    ):
        candidates.append(getattr(settings, attr, None))
    for w in getattr(settings, "discord_webhooks", []) or []:
        candidates.append(w)
    # AUDIT S8: unwrap SecretStr values before scrubbing.
    from config.settings import reveal_secret

    revealed = [reveal_secret(c) for c in candidates]
    return [c for c in revealed if c and len(c) >= 6]


def configure_logging(settings: Any, level: str = "INFO") -> None:
    """Configure structlog. Any known secret value in a log line is masked."""
    secrets = _collect_secrets(settings)

    def mask_secrets_processor(logger_: Any, method_name: str, event_dict: Any) -> Any:
        event = event_dict.get("event")
        if isinstance(event, str):
            event_dict["event"] = mask_text(event, secrets)
        for key, value in list(event_dict.items()):
            if isinstance(value, str):
                event_dict[key] = mask_text(value, secrets)
            elif isinstance(value, (dict, list)):
                event_dict[key] = mask_sensitive(value)
        return event_dict

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # P1-13: RUNBOOK promises readable logs with LOG_JSON=false.
    renderer = (
        structlog.processors.JSONRenderer()
        if getattr(settings, "log_json", True)
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            mask_secrets_processor,
            renderer,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
