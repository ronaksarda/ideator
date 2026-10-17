import logging
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger("ideator.telemetry")


@contextmanager
def agent_span(
    op: str,
    description: str,
    data: Optional[Dict[str, Any]] = None
) -> Generator[Any, None, None]:
    """
    Sentry span for one agent step (a Gemma call or an MCP tool call), nested under the turn's transaction.
    Does nothing when Sentry is not configured.
    """
    span = None
    try:
        import sentry_sdk
        # Check if sentry is actively initialized
        if sentry_sdk.is_initialized():
            # A child of the current span; a bare start_span() is never attached to the transaction and gets dropped.
            parent = sentry_sdk.get_current_span()
            span = parent.start_child(op=op, name=description) if parent else sentry_sdk.start_span(op=op, name=description)
            span.__enter__()
            if data:
                for k, v in data.items():
                    span.set_data(k, v)
    except Exception:
        span = None

    try:
        yield span
    except Exception as exc:
        if span:
            span.set_status("internal_error")
            span.set_data("error", str(exc))
        raise
    finally:
        if span:
            try:
                span.__exit__(None, None, None)
            except Exception:
                pass
