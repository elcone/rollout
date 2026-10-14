import hashlib
import importlib.util
import logging
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

import duckdb

from rollout.execution import (
    ExecutionStatus,
    ModelResult,
    execute_dataset,
    quote_identifier,
)
from rollout.models import STAGING_SCHEMA, ModelId, Seeder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeederResult:
    status: ExecutionStatus
    error: str | None
    execution_duration: float | None
    total_duration: float
    output_models: dict[ModelId, ModelResult]


def run_seeder(
    seeder: Seeder,
    connection: duckdb.DuckDBPyConnection,
    staging_dir: Path,
) -> dict[str, duckdb.DuckDBPyRelation]:
    try:
        path_hash = hashlib.sha256(str(seeder.path.resolve()).encode()).hexdigest()
        module_name = f"_rollout_seeder_{path_hash}"
        spec = importlib.util.spec_from_file_location(module_name, seeder.path)
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load Python module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)

            run = getattr(module, "run", None)
            if not callable(run):
                raise ValueError(
                    "Seeder must define a callable run(connection, staging_dir)"
                )

            outputs = run(connection, staging_dir)
            if type(outputs) is not dict:
                raise ValueError(
                    f"run() must return a Python dict, got {type(outputs).__name__}"
                )

            missing = [name for name in seeder.config.outputs if name not in outputs]
            unexpected = [name for name in outputs if name not in seeder.config.outputs]
            if missing or unexpected:
                raise ValueError(
                    f"run() output names do not match declarations: "
                    f"missing outputs {missing!r}; unexpected outputs {unexpected!r}"
                )

            for name in seeder.config.outputs:
                value = outputs[name]
                if not isinstance(value, duckdb.DuckDBPyRelation):
                    raise ValueError(
                        f"Output {name!r} must be DuckDBPyRelation, "
                        f"got {type(value).__name__}"
                    )

            return {name: outputs[name] for name in seeder.config.outputs}
        finally:
            sys.modules.pop(module_name, None)
    except Exception as exc:
        raise RuntimeError(f"Seeder execution failed for {seeder.path}: {exc}") from exc


def _materialize_output(
    connection: duckdb.DuckDBPyConnection,
    identity: ModelId,
    relation: duckdb.DuckDBPyRelation,
) -> None:
    source = f"_rollout_seeder_output_{uuid4().hex}"
    connection.register(source, relation)
    try:
        target = (
            f"{quote_identifier(identity.schema)}.{quote_identifier(identity.name)}"
        )
        connection.execute(
            f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {quote_identifier(source)}"
        )
    finally:
        connection.unregister(source)


def execute_seeder(
    seeder: Seeder,
    connection: duckdb.DuckDBPyConnection,
) -> SeederResult:
    started = perf_counter()
    logger.info("Executing seeder %s", seeder.path)
    logger.debug("Declared outputs: %s", ", ".join(map(str, seeder.output_ids)))
    error = None
    results: dict[ModelId, ModelResult] = {}
    with TemporaryDirectory(prefix="rollout-seeder-") as directory:
        logger.debug("Temporary staging directory: %s", directory)
        execution_started = perf_counter()
        try:
            outputs = run_seeder(seeder, connection, Path(directory))
        except RuntimeError as exc:
            error = str(exc)
            outputs = {}
        execution_duration = perf_counter() - execution_started
        if error is None:
            connection.execute(
                f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(STAGING_SCHEMA)}"
            )
        for name, relation in outputs.items():
            identity = ModelId(STAGING_SCHEMA, name)
            results[identity] = execute_dataset(
                connection,
                identity,
                partial(_materialize_output, connection, identity, relation),
                seeder.config.outputs[name].quality,
            )
    status = (
        ExecutionStatus.FAILED
        if error is not None
        or any(result.status is ExecutionStatus.FAILED for result in results.values())
        else ExecutionStatus.SUCCESS
    )
    logger.info("Seeder %s %s", seeder.path, status.value.lower())
    if error is not None:
        logger.error("%s", error)
    logger.debug("Temporary staging cleaned for %s", seeder.path)
    return SeederResult(
        status, error, execution_duration, perf_counter() - started, results
    )
