"""Structured JSON logs on stdout, which Lambda ships to CloudWatch."""

import sys

from loguru import logger


def setup_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stdout, level=level.upper(), serialize=True, backtrace=False, diagnose=False)
