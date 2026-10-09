"""Package logging; console output is configured only by the CLI."""

import logging
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager

from rollout.context import execution_id, operation


class ContextFilter(logging.Filter):
    """Attach operation metadata to log records without rejecting them."""
    def filter(self, record: logging.LogRecord) -> bool:
        """Enrich a record with the current execution ID and operation.

        Args:
            record: Log record to modify in place.

        Returns:
            True to retain the record. Missing context fields are represented by -.
        """
        record.execution_id = execution_id.get() or "-"
        record.operation = operation.get() or "-"
        return True


_context_filter = ContextFilter()


def get_logger(name: str) -> logging.Logger:
    """Get a module logger enriched with execution context.

    Args:
        name: Logger name, normally __name__ within the rollout hierarchy.

    Returns:
        A logger with the shared context filter. No handlers or levels are changed.
    """
    logger = logging.getLogger(name)
    logger.addFilter(_context_filter)
    return logger


@contextmanager
def cli_logging(verbose: bool = False) -> Generator[None, None, None]:
    """Temporarily route package logs to stderr for one CLI invocation.

    The package logger's handlers, level, and propagation setting are restored
    on exit. The application's root logger is not modified. This configuration
    is intended for sequential CLI invocations, not concurrent reconfiguration.

    Args:
        verbose: Whether to include DEBUG messages instead of only INFO and above.

    Yields:
        None while the CLI handler and formatter are installed.
    """
    logger = logging.getLogger("rollout")
    previous = logger.handlers[:], logger.level, logger.propagate

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_context_filter)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s [%(execution_id)s] "
        "%(name)s (%(operation)s): %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)

    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    try:
        yield

    finally:
        logger.handlers, level, logger.propagate = previous
        logger.setLevel(level)
        handler.close()
