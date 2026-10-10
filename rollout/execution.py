import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import duckdb

from rollout.config import EngineConfig, LoadStrategy
from rollout.dag import DAG
from rollout.models import Model, ModelId

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ModelResult:
    status: ExecutionStatus
    error: str | None = None


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect(engine_config: EngineConfig) -> duckdb.DuckDBPyConnection:
    path = engine_config.warehouse_path
    path.parent.mkdir(parents=True, exist_ok=True)

    return duckdb.connect(str(path))


def prepare_schemas(connection: duckdb.DuckDBPyConnection, dag: DAG) -> None:
    schemas = {identity.schema for identity in dag.models} | {"public"}
    for schema in sorted(schemas):
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_identifier(schema)}")
    connection.execute("SET search_path = 'public,main'")


def build_full_sql(model: Model) -> str:
    schema = _quote_identifier(model.id.schema)
    table = _quote_identifier(model.id.name)

    return f"CREATE OR REPLACE TABLE {schema}.{table} AS\n{model.sql}"


def build_model_sql(model: Model) -> str:
    if model.config.load_strategy is LoadStrategy.FULL:
        return build_full_sql(model)

    raise ValueError(
        f"Unsupported load strategy for {model.id}: {model.config.load_strategy}"
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
    connection.execute("BEGIN TRANSACTION")
    logger.debug("Transaction started for %s", model.id)

    try:
        connection.execute(sql)
        connection.execute("COMMIT")

    except duckdb.Error as exc:
        connection.execute("ROLLBACK")
        logger.debug("Transaction rolled back for %s", model.id)
        logger.error("Failed %s: %s", model.id, exc)
        return ModelResult(ExecutionStatus.FAILED, str(exc))

    else:
        logger.debug("Transaction committed for %s", model.id)
        logger.info("Materialized %s", model.id)
        return ModelResult(ExecutionStatus.SUCCESS)


def execute_models(
    dag: DAG,
    engine_config: EngineConfig | None = None,
) -> dict[ModelId, ModelResult]:
    if engine_config is None:
        engine_config = EngineConfig()

    path = engine_config.warehouse_path
    logger.info("Starting execution of %d models in %s", len(dag.order), path)
    results: dict[ModelId, ModelResult] = {}

    with connect(engine_config) as connection:
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
            sql = build_model_sql(model)
            sql_path = run_dir / identity.schema / f"{identity.name}.sql"
            save_generated_sql(sql, sql_path)
            results[identity] = execute_model(connection, model, sql)

    return results
