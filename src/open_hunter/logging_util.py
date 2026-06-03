"""Project-wide logging, rendered nicely to the console and to a log file."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(logs_dir: Path, verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = logging.DEBUG if verbose else logging.INFO
    log_file = logs_dir / "open_hunter.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    file_handler.setLevel(logging.DEBUG)

    console_handler = RichHandler(
        rich_tracebacks=True, show_path=False, markup=False
    )
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Quiet down noisy third-party libraries.
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
