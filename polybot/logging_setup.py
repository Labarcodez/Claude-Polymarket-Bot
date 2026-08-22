"""Logging configuration shared by the CLI and the trading engine."""
from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: str = "INFO", log_path: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

    # Keep third-party HTTP libraries from drowning out our own logs.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
