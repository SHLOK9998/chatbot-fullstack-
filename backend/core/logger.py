# core/logger.py
# core/logger.py intialise in main.py and use throughout in the all modules using logging.getlogger(__name__)

import logging
from pathlib import Path

# LOG_DIR = Path("logs")
# LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logger(level: int = logging.INFO) -> None:
    """
    Configure root logger with:
      - Console (stdout) handler
    Safe to call multiple times (handlers are not duplicated).
    """
    root = logging.getLogger()

    # Avoid adding duplicate handlers on reload / test re-runs
    if root.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(console_handler)

    logging.getLogger(__name__).info("Logger initialised ")
    