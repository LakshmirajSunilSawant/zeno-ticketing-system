# WHY structlog + JSON: Render (and any log aggregator - Datadog, CloudWatch, Loki) indexes JSON
# fields. A line like `{"event":"request","request_id":...,"latency_ms":...}` is queryable;
# "GET /bookings 200 12ms" is a string I'd have to regex. Same reason you'd never log free text
# in production.
import logging
import sys

import structlog


def configure_logging(env: str = "development") -> None:
    processors = [
        structlog.contextvars.merge_contextvars,  # WHY: pulls in request_id bound by the middleware
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    # WHY branch on env: JSON is for machines in prod; colourised console output is for me locally.
    if env == "production":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "app"):
    return structlog.get_logger(name)
