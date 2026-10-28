"""Logs: console lisible + fichier JSON lines structure, avec masquage systematique des secrets."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Motifs masques meme s'ils ne figurent pas dans la liste explicite des secrets.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),  # cle privee EVM (une adresse fait 40 hex, non masquee)
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for p in _PATTERNS:
            text = p.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = self.redact(msg)
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event_id", "market", "stage", "latency_s"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_text:
            data["exc"] = record.exc_text
        return json.dumps(data, ensure_ascii=False)


def setup_logging(secrets: list[str], log_dir: str = "logs", level: int = logging.INFO) -> None:
    Path(log_dir).mkdir(exist_ok=True)
    redactor = RedactingFilter(secrets)
    file_handler = RotatingFileHandler(f"{log_dir}/bot.jsonl", maxBytes=20_000_000, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(redactor)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console.addFilter(redactor)
    root = logging.getLogger()
    root.handlers[:] = [file_handler, console]
    root.setLevel(level)
    for noisy in ("httpx", "websockets", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
