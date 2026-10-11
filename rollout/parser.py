import logging
from collections.abc import Iterable
from dataclasses import replace

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from rollout.models import Model, ModelId

logger = logging.getLogger(__name__)


def validate_delete_predicate(predicate: str) -> None:
    wrapper = "SELECT 1 FROM __rollout_target__ AS target"
    try:
        statements = sqlglot.parse(f"{wrapper} WHERE {predicate}", read="duckdb")
        if len(statements) != 1:
            raise ValueError("expected exactly one SQL statement")
        statement = statements[0]
        if not isinstance(statement, exp.Select) or not statement.args.get("where"):
            raise ValueError("expected a WHERE expression in a SELECT")
        without_where = statement.copy()
        without_where.set("where", None)
        if without_where != sqlglot.parse_one(wrapper, read="duckdb"):
            raise ValueError("predicate must contain only a WHERE expression")
        if any(
            isinstance(node, (exp.DML, exp.DDL, exp.Into)) for node in statement.walk()
        ):
            raise ValueError("predicate must not contain data modifications")
    except (SqlglotError, ValueError) as exc:
        raise ValueError(f"Invalid delete_insert.where: {exc}") from exc


def extract_dependencies(sql: str) -> frozenset[ModelId] | None:
    statements = [
        statement
        for statement in sqlglot.parse(sql, read="duckdb")
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]

    if not statements:
        return None

    if len(statements) != 1:
        raise ValueError("SQL models must contain exactly one query statement")

    statement = statements[0]
    if not isinstance(statement, (exp.Query, exp.Values, exp.Pivot)) or any(
        isinstance(node, (exp.DML, exp.DDL, exp.Into)) for node in statement.walk()
    ):
        raise ValueError("SQL models must contain only a read-only query")

    dependencies = set()
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
    logger.info("Parsing SQL model dependencies")
    parsed = []
    for model in models:
        try:
            dependencies = extract_dependencies(model.sql)

        except (SqlglotError, ValueError) as exc:
            raise ValueError(f"Invalid SQL in {model.path}: {exc}") from exc

        if dependencies is None:
            logger.warning("Ignoring empty SQL model: %s", model.path)
            continue

        logger.debug("Parsed %s: %d dependencies", model.id, len(dependencies))
        parsed.append(replace(model, dependencies=dependencies))

    logger.info("Parsed %d models", len(parsed))

    return parsed
