"""Structured JSON logs on stdout, which Lambda ships to CloudWatch; plain text lines in
local mode, where a person reads them."""

import sys

from loguru import logger


def setup_logging(level: str, *, readable: bool = False) -> None:
    logger.remove()
    logger.add(
        sys.stdout, level=level.upper(), serialize=not readable, backtrace=False, diagnose=False
    )
