"""DuckDB SQL parsing and scope-aware table dependency extraction."""

from collections.abc import Iterable
from dataclasses import replace

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from rollout.logging import get_logger
from rollout.models import Model, ModelId

logger = get_logger(__name__)


def extract_dependencies(sql: str) -> frozenset[ModelId]:
    """Extract physical table references from DuckDB SQL queries.

    CTEs and subquery aliases are excluded. Standalone PIVOT inputs are
    normalized for scope analysis. Unqualified tables use public, while
    explicit identifiers retain their case and catalog qualification.

    Args:
        sql: SQL text containing one or more query statements.

    Returns:
        Distinct referenced table identities, possibly empty for constant queries.

    Raises:
        ValueError: SQL is empty or contains a non-query statement.
        SqlglotError: SQLGlot cannot parse or analyze the SQL.
    """
    statements = [
        statement
        for statement in sqlglot.parse(sql, read="duckdb")
        if statement is not None
    ]

    if not statements:
        raise ValueError("SQL model is empty")

    dependencies = set()
    for statement in statements:
        if not isinstance(statement, exp.Query):
            raise ValueError("SQL models must contain SELECT queries")  # noqa: TRY004

        # DuckDB's standalone PIVOT is not traversed by SQLGlot's scope
        # builder. Represent its input as a SELECT for dependency analysis
        # so CTE names still resolve to scopes rather than physical tables.
        for pivot in list(statement.find_all(exp.Pivot)):
            if pivot.this is None:
                continue
            query = exp.select(
                "*",
                *(expression.copy() for expression in pivot.expressions),
                *(expression.copy() for expression in pivot.args.get("using", [])),
            ).from_(pivot.this.copy())

            if pivot.args.get("with_") is not None:
                query.set("with_", pivot.args["with_"].copy())

            if pivot is statement:
                statement = query

            else:
                pivot.replace(query)

        for scope in traverse_scope(statement):
            for source in scope.sources.values():
                if isinstance(source, exp.Table) and isinstance(
                    source.this, exp.Identifier
                ):
                    dependencies.add(
                        ModelId(source.db or "public", source.name, source.catalog)
                    )

    return frozenset(dependencies)


def parse_models(models: Iterable[Model]) -> list[Model]:
    """Populate dependencies on copies of discovered model records.

    Args:
        models: Model records whose SQL should be analyzed.

    Returns:
        New model records in input order; input records are not modified.

    Raises:
        ValueError: A model's SQL is invalid. The error includes its source path.
    """
    logger.info("Parsing SQL model dependencies")
    parsed = []
    for model in models:
        try:
            dependencies = extract_dependencies(model.sql)

        except (SqlglotError, ValueError) as exc:
            raise ValueError(f"Invalid SQL in {model.path}: {exc}") from exc

        logger.debug("Parsed %s: %d dependencies", model.id, len(dependencies))
        parsed.append(replace(model, dependencies=dependencies))

    logger.info("Parsed %d models", len(parsed))

    return parsed
