from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from functools import cache
from importlib.resources import files
from numbers import Real

import duckdb

from rollout.config import (
    CheckSeverity,
    NullnessCheck,
    QualityCheck,
    RowCountCheck,
    UniquenessCheck,
)
from rollout.models import Model, ModelId


class CheckStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class QualityCheckResult:
    check: QualityCheck
    status: CheckStatus
    severity: CheckSeverity
    observed_value: int | float | Decimal
    message: str | None = None


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _target(identity: ModelId) -> str:
    return f"{_quote_identifier(identity.schema)}.{_quote_identifier(identity.name)}"


@cache
def _load_quality_template(name: str) -> str:
    return (
        files("rollout")
        .joinpath("templates", "quality", name)
        .read_text(encoding="utf-8")
        .strip()
    )


def build_row_count_sql(identity: ModelId) -> str:
    return _load_quality_template("row_count.sql").format(target=_target(identity))


def build_nullness_sql(identity: ModelId, column: str) -> str:
    return _load_quality_template("nullness.sql").format(
        target=_target(identity),
        column=_quote_identifier(column),
    )


def build_uniqueness_sql(identity: ModelId, columns: tuple[str, ...]) -> str:
    keys = ", ".join(_quote_identifier(column) for column in columns)
    return _load_quality_template("uniqueness.sql").format(
        target=_target(identity),
        columns=keys,
    )


def observe_numeric_scalar(
    connection: duckdb.DuckDBPyConnection, sql: str
) -> int | float | Decimal:
    cursor = connection.execute(sql)

    if len(cursor.description) != 1:
        raise ValueError("Quality query must return exactly one column")
    rows = cursor.fetchmany(2)

    if len(rows) != 1:
        raise ValueError("Quality query must return exactly one row")
    value = rows[0][0]

    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(
            "Quality query must return a numeric scalar (booleans are unsupported)"
        )

    return value


def evaluate_check(
    check: QualityCheck,
    observed_value: int | float | Decimal,
) -> bool:
    if isinstance(check, RowCountCheck):
        return observed_value >= check.min
    elif isinstance(check, NullnessCheck):
        return observed_value <= check.max
    elif isinstance(check, UniquenessCheck):
        return observed_value <= check.max
    else:
        raise ValueError(f"Unsupported quality check: {type(check).__name__}")


def run_quality_checks(
    connection: duckdb.DuckDBPyConnection, model: Model
) -> Iterator[QualityCheckResult]:
    for check in model.config.quality:
        if isinstance(check, RowCountCheck):
            sql = build_row_count_sql(model.id)
        elif isinstance(check, NullnessCheck):
            sql = build_nullness_sql(model.id, check.column)
        elif isinstance(check, UniquenessCheck):
            sql = build_uniqueness_sql(model.id, check.columns)
        else:
            raise ValueError(f"Unsupported quality check: {type(check).__name__}")

        try:
            observed = observe_numeric_scalar(connection, sql)
        except (duckdb.Error, ValueError) as exc:
            raise ValueError(
                f"Quality check execution error ({check.type}): {exc}"
            ) from exc

        passed = evaluate_check(check, observed)

        yield QualityCheckResult(
            check,
            CheckStatus.PASS if passed else CheckStatus.FAIL,
            check.severity,
            observed,
        )
