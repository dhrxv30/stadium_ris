"""Logging setup: console (configurable level) + full DEBUG log file in the run directory."""
import logging
from pathlib import Path


def setup_logging(name: str, level: str = "INFO", run_dir=None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):      # idempotent across repeated calls (tests)
        logger.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level))
    console.setFormatter(fmt)
    logger.addHandler(console)
    if run_dir is not None:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(Path(run_dir) / f"{name}.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger
