import logging
import os
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler


LOG_DIR = "logs"
MSK_TZ = timezone(timedelta(hours=3), name="MSK")

_configured = False


class MskFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, MSK_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def _build_file_handler(path: str, level: int = logging.INFO) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        filename=path,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(MskFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    return handler


def _configure_named_logger(name: str, filename: str, level: int = logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = True

    file_path = os.path.join(LOG_DIR, filename)
    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "").endswith(filename) for h in logger.handlers):
        logger.addHandler(_build_file_handler(file_path, level=level))


def setup_logging():
    global _configured
    if _configured:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(MskFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(console)

    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "").endswith("bot.log") for h in root.handlers):
        root.addHandler(_build_file_handler(os.path.join(LOG_DIR, "bot.log")))
    if not any(isinstance(h, TimedRotatingFileHandler) and getattr(h, "baseFilename", "").endswith("errors.log") for h in root.handlers):
        root.addHandler(_build_file_handler(os.path.join(LOG_DIR, "errors.log"), level=logging.ERROR))

    _configure_named_logger("ai", "ai.log")
    _configure_named_logger("funnel", "funnel.log")
    _configure_named_logger("payments", "payments.log")
    _configure_named_logger("webhook", "webhook.log")
    _configure_named_logger("vector", "vector.log")

    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
