"""App Insights (OpenTelemetry) bootstrap + usage-event emission.

``setup_telemetry`` is called from BOTH entrypoints — api.py (API app) and
async_pipeline_v2/worker_main.py (worker fleet). Without a connection string it
is a no-op and the process runs exactly as before.
"""
from __future__ import annotations

import logging
from typing import Optional

_USAGE_EVENT_NAME = "edenn.usage"
_configured = False

_event_logger = logging.getLogger("edenn.usage")


def setup_telemetry(
    *, connection_string: Optional[str], role: str, logger: logging.Logger
) -> bool:
    """Configure Azure Monitor OTel once per process. False = disabled."""
    global _configured
    if _configured:
        return True
    if not connection_string:
        logger.info("Telemetry disabled: APPLICATIONINSIGHTS_CONNECTION_STRING unset.")
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=connection_string)
    except Exception as exc:  # noqa: BLE001 - telemetry must never block boot
        logger.warning("Telemetry disabled: configure_azure_monitor failed: %s", exc)
        return False
    _configured = True
    logger.info("Telemetry enabled (role=%s): Azure Monitor OpenTelemetry.", role)
    return True


def emit_usage_event(attributes: dict) -> None:
    """Emit one usage record as an App Insights custom event. Never raises.

    The ``microsoft.custom_event.name`` attribute makes the Azure Monitor
    exporter surface the record in the customEvents table; without telemetry
    configured this is just a low-noise structured log line.
    """
    try:
        _event_logger.log(
            logging.INFO if _configured else logging.DEBUG,
            "edenn.usage job_id=%s user_id=%s total_cost_usd=%s",
            attributes.get("edenn.job_id"),
            attributes.get("edenn.user_id"),
            attributes.get("edenn.total_cost_usd"),
            extra={"microsoft.custom_event.name": _USAGE_EVENT_NAME, **attributes},
        )
    except Exception:  # noqa: BLE001 - usage emission must never raise
        logging.getLogger(__name__).warning(
            "usage: event emission failed", exc_info=True
        )


__all__ = ["emit_usage_event", "setup_telemetry"]
