import operator
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from numbers import Real

import duckdb
from sqlglot.errors import SqlglotError

from rollout.config import (
    CheckSeverity,
    CustomSqlCheck,
    ExpectationOperator,
    NullnessCheck,
    QualityCheck,
    RowCountCheck,
    UniquenessCheck,
)
from rollout.models import Model, ModelId
from rollout.parser import parse_read_only_query
from rollout.sql_templates import sql_templates

OPERATORS = {
    ExpectationOperator.GT: operator.gt,
    ExpectationOperator.GTE: operator.ge,
    ExpectationOperator.EQ: operator.eq,
    ExpectationOperator.NEQ: operator.ne,
    ExpectationOperator.LT: operator.lt,
    ExpectationOperator.LTE: operator.le,
}


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


def build_row_count_sql(identity: ModelId) -> str:
    return sql_templates.get_template("quality/row_count.sql.j2").render(
        target=_target(identity)
    )


def build_nullness_sql(identity: ModelId, column: str) -> str:
    return sql_templates.get_template("quality/nullness.sql.j2").render(
        target=_target(identity),
        column=_quote_identifier(column),
    )


def build_uniqueness_sql(identity: ModelId, columns: tuple[str, ...]) -> str:
    keys = ", ".join(_quote_identifier(column) for column in columns)
    return sql_templates.get_template("quality/uniqueness.sql.j2").render(
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


def normalize_number(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def evaluate_check(
    check: QualityCheck,
    observed_value: int | float | Decimal,
) -> bool:
    if isinstance(check, CustomSqlCheck):
        observed = normalize_number(observed_value)
        return all(
            OPERATORS[expectation.operator](
                observed, normalize_number(expectation.value)
            )
            for expectation in check.expectations
        )
    elif isinstance(check, RowCountCheck):
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
        if isinstance(check, CustomSqlCheck):
            sql = check.sql
        elif isinstance(check, RowCountCheck):
            sql = build_row_count_sql(model.id)
        elif isinstance(check, NullnessCheck):
            sql = build_nullness_sql(model.id, check.column)
        elif isinstance(check, UniquenessCheck):
            sql = build_uniqueness_sql(model.id, check.columns)
        else:
            raise ValueError(f"Unsupported quality check: {type(check).__name__}")

        try:
            if isinstance(check, CustomSqlCheck):
                if parse_read_only_query(sql) is None:
                    raise ValueError("Custom SQL must contain one read-only query")
            observed = observe_numeric_scalar(connection, sql)
        except (duckdb.Error, ValueError, SqlglotError) as exc:
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
