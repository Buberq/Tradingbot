"""Logowanie: konsola + rotowany plik, z maskowaniem sekretów.

Filtr podmienia każde wystąpienie znanej wartości sekretu na "***", więc nawet
przypadkowe zalogowanie klucza czy tokenu nie trafi do pliku ani na konsolę.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from krakenbot.config import Settings

LOGGER_NAME = "krakenbot"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MIN_SECRET_LEN = 6  # krótszych wartości nie maskujemy, żeby nie psuć zwykłego tekstu


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self._secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for s in self._secrets:
                record.exc_text = record.exc_text.replace(s, "***")
        return True


def setup_logging(settings: Settings) -> logging.Logger:
    log_dir: Path = settings.paths.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.logging.level.upper())
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    redact = RedactSecretsFilter(settings.secrets.present_values())
    fmt = logging.Formatter(_FORMAT)

    file_path = log_dir / "krakenbot.log"
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h.addFilter(redact)
        logger.addHandler(h)
    os.chmod(file_path, 0o600)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
