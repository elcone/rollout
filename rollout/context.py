"""Context shared by all modules during a public API operation."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from secrets import token_hex

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

execution_id: ContextVar[str | None] = ContextVar("rollout_execution_id", default=None)
operation: ContextVar[str | None] = ContextVar("rollout_operation", default=None)


def new_execution_id() -> str:
    """Generate a human-readable operation ID using the current UTC time.

    Returns:
        An ID in YYYY-MM-DD_Ddd_HH-mm-ssZ_hex format. The six random
        hexadecimal characters reduce collisions without guaranteeing uniqueness.
    """
    now = datetime.now(UTC)
    return f"{now:%Y-%m-%d}_{_WEEKDAYS[now.weekday()]}_{now:%H-%M-%S}Z_{token_hex(3)}"


@contextmanager
def execution_context(identifier: str, name: str) -> Generator[None, None, None]:
    """Scope log metadata to an operation and restore the previous context.

    Nested contexts restore their parent's values, including on exceptions.
    Values are local to the current ContextVar context.

    Args:
        identifier: Non-empty execution ID to attach to log records.
        name: Operation name, such as dag.

    Yields:
        None while the execution ID and operation name are active.

    Raises:
        ValueError: The identifier is not a non-empty string.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("execution_id must be a non-empty string")

    id_token = execution_id.set(identifier)
    operation_token = operation.set(name)

    try:
        yield

    finally:
        operation.reset(operation_token)
        execution_id.reset(id_token)
