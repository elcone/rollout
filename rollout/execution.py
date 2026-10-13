import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from pathlib import Path
from time import perf_counter

import duckdb

from rollout.config import CheckSeverity, EngineConfig, LoadStrategy, QualityCheck
from rollout.dag import DAG
from rollout.models import Model, ModelId
from rollout.parser import validate_delete_predicate
from rollout.quality import CheckStatus, QualityCheckResult, run_dataset_quality_checks
from rollout.sql_templates import sql_templates

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ModelResult:
    status: ExecutionStatus
    error: str | None = None
    duration: float | None = None
    quality_results: tuple[QualityCheckResult, ...] = ()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect(engine_config: EngineConfig) -> duckdb.DuckDBPyConnection:
    path = engine_config.warehouse_path
    path.parent.mkdir(parents=True, exist_ok=True)

    return duckdb.connect(str(path))


def prepare_schemas(connection: duckdb.DuckDBPyConnection, dag: DAG) -> None:
    schemas = {identity.schema for identity in dag.models} | {"public"}
    for schema in sorted(schemas):
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
    connection.execute("SET search_path = 'public,main'")


def build_full_sql(model: Model) -> str:
    schema = quote_identifier(model.id.schema)
    table = quote_identifier(model.id.name)

    return sql_templates.get_template("load_strategies/full.sql.j2").render(
        target=f"{schema}.{table}", model_sql=model.sql
    )


def build_delete_insert_sql(model: Model, predicate: str) -> str:
    schema = quote_identifier(model.id.schema)
    table = quote_identifier(model.id.name)
    return sql_templates.get_template("load_strategies/delete_insert.sql.j2").render(
        target=f"{schema}.{table}", where=predicate, model_sql=model.sql
    )


def target_table_exists(connection: duckdb.DuckDBPyConnection, model: Model) -> bool:
    return connection.execute(
        sql_templates.get_template("misc/table_exists.sql").render(),
        [model.id.schema, model.id.name],
    ).fetchone()[0]


def build_model_sql(model: Model, *, target_exists: bool = False) -> str:
    strategy = model.config.load_strategy
    if strategy is LoadStrategy.FULL:
        return build_full_sql(model)

    if strategy is LoadStrategy.DELETE_INSERT:
        predicate = model.config.delete_insert.where.strip()
        validate_delete_predicate(predicate)

    if not target_exists:
        return build_bootstrap_sql(model)

    if strategy is LoadStrategy.APPEND:
        schema = quote_identifier(model.id.schema)
        table = quote_identifier(model.id.name)
        return sql_templates.get_template("load_strategies/append.sql.j2").render(
            target=f"{schema}.{table}", model_sql=model.sql
        )

    if strategy is LoadStrategy.DELETE_INSERT:
        return build_delete_insert_sql(model, predicate)

    raise ValueError(
        f"Unsupported load strategy for {model.id}: {model.config.load_strategy}"
    )


def build_bootstrap_sql(model: Model) -> str:
    schema = quote_identifier(model.id.schema)
    table = quote_identifier(model.id.name)
    return sql_templates.get_template("load_strategies/bootstrap.sql.j2").render(
        target=f"{schema}.{table}", model_sql=model.sql
    )


def save_generated_sql(sql: str, sql_path: Path) -> None:
    try:
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path.write_text(sql, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save generated SQL to %s: %s", sql_path, exc)
    else:
        logger.debug("Generated SQL saved to %s", sql_path)


def execute_model(
    connection: duckdb.DuckDBPyConnection,
    model: Model,
    sql: str,
) -> ModelResult:
    logger.debug(
        "Executing %s from %s (%s)",
        model.id,
        model.path,
        model.config.load_strategy.value,
    )
    logger.debug(
        "Dependencies for %s: %s",
        model.id,
        ", ".join(map(str, sorted(model.dependencies))) or "None",
    )
    return execute_dataset(
        connection, model.id, partial(connection.execute, sql), model.config.quality
    )


def execute_dataset(
    connection: duckdb.DuckDBPyConnection,
    identity: ModelId,
    materialize: Callable[[], object],
    quality: tuple[QualityCheck, ...],
) -> ModelResult:
    started = perf_counter()
    connection.execute("BEGIN TRANSACTION")
    logger.debug("Transaction started for %s", identity)

    quality_results: list[QualityCheckResult] = []
    try:
        materialize()
        quality_results.extend(
            run_dataset_quality_checks(connection, identity, quality)
        )
        failures = [
            result
            for result in quality_results
            if result.status is CheckStatus.FAIL
            and result.severity is CheckSeverity.ERROR
        ]
        if failures:
            raise ValueError(
                "Quality rule failed: "
                + "; ".join(
                    f"{result.check.type}: observed {result.observed_value}, "
                    f"expected {result.check}"
                    for result in failures
                )
            )
        connection.execute("COMMIT")

    except (duckdb.Error, ValueError) as exc:
        connection.execute("ROLLBACK")
        duration = perf_counter() - started
        logger.debug("Transaction rolled back for %s", identity)
        logger.error("Failed %s: %s", identity, exc)

        return ModelResult(
            ExecutionStatus.FAILED, str(exc), duration, tuple(quality_results)
        )

    else:
        duration = perf_counter() - started
        logger.debug("Transaction committed for %s", identity)
        logger.info("Materialized %s", identity)

        return ModelResult(
            ExecutionStatus.SUCCESS,
            duration=duration,
            quality_results=tuple(quality_results),
        )


def execute_models(
    dag: DAG,
    engine_config: EngineConfig | None = None,
) -> dict[ModelId, ModelResult]:
    if engine_config is None:
        engine_config = EngineConfig()

    with connect(engine_config) as connection:
        return execute_models_with_connection(connection, dag, engine_config)


def execute_models_with_connection(
    connection: duckdb.DuckDBPyConnection,
    dag: DAG,
    engine_config: EngineConfig,
) -> dict[ModelId, ModelResult]:
    path = engine_config.warehouse_path
    logger.info("Starting execution of %d models in %s", len(dag.order), path)
    results: dict[ModelId, ModelResult] = {}

    prepare_schemas(connection, dag)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = path.parent / "models" / timestamp
    logger.info("Generated model SQL directory: %s", run_dir)

    for identity in dag.order:
        local_dependencies = dag.dependencies[identity] & dag.internal_nodes
        blocked_dependencies = sorted(
            dependency
            for dependency in local_dependencies
            if results[dependency].status is not ExecutionStatus.SUCCESS
        )

        if blocked_dependencies:
            logger.info("Skipped %s: a local dependency did not succeed", identity)
            logger.debug(
                "Blocking dependencies for %s: %s",
                identity,
                ", ".join(
                    f"{dependency} ({results[dependency].status.value})"
                    for dependency in blocked_dependencies
                ),
            )
            results[identity] = ModelResult(ExecutionStatus.SKIPPED)
            continue

        model = dag.models[identity]
        strategy = model.config.load_strategy
        if strategy is LoadStrategy.FULL:
            sql = build_model_sql(model)
        else:
            sql = build_model_sql(
                model, target_exists=target_table_exists(connection, model)
            )
        sql_path = run_dir / identity.schema / f"{identity.name}.sql"
        save_generated_sql(sql, sql_path)
        results[identity] = execute_model(connection, model, sql)

    return results
