import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from rollout.config import (
    CustomSqlCheck,
    NullnessCheck,
    QualityCheck,
    RowCountCheck,
    UniquenessCheck,
)
from rollout.dag import DAG
from rollout.execution import ExecutionStatus, ModelResult
from rollout.models import ModelId
from rollout.quality import quality_check_details
from rollout.seeders import SeederResult


def _quality_parameters(check: QualityCheck) -> dict:
    if isinstance(check, RowCountCheck):
        return {"min": check.min}
    if isinstance(check, NullnessCheck):
        return {"column": check.column, "max": check.max}
    if isinstance(check, UniquenessCheck):
        return {"columns": list(check.columns), "max": check.max}
    if isinstance(check, CustomSqlCheck):
        return {
            "expectations": [
                {"operator": item.operator.value, "value": item.value}
                for item in check.expectations
            ]
        }
    raise ValueError(f"Unsupported quality check: {type(check).__name__}")


def _json_value(value: object) -> str | int | float:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Cannot serialize {type(value).__name__} to run state")


def _model_id(identity: ModelId) -> dict[str, str]:
    return {"schema": identity.schema, "name": identity.name}


def persist_run_state(
    state_path: Path,
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    total_duration: float,
    dag: DAG,
    results: dict[ModelId, ModelResult],
    seeder_results: dict[str, SeederResult],
) -> None:
    """Append one complete run document; callers handle non-critical write failures."""
    dataset_results = [
        *results.items(),
        *(
            item
            for seeder in seeder_results.values()
            for item in seeder.output_models.items()
        ),
    ]
    all_results = [*seeder_results.values(), *(result for _, result in dataset_results)]
    status = (
        ExecutionStatus.FAILED
        if any(result.status is ExecutionStatus.FAILED for result in all_results)
        else ExecutionStatus.SUCCESS
    )
    quality = []
    for identity, result in dataset_results:
        if not result.quality_results:
            continue
        checks = []
        for observation in result.quality_results:
            name, expected = quality_check_details(observation.check)
            checks.append(
                {
                    "type": observation.check.type,
                    "name": name,
                    "parameters": _quality_parameters(observation.check),
                    "observed": observation.observed_value,
                    "expected": expected,
                    "status": observation.status.value,
                    "severity": observation.severity.value,
                    "duration": observation.duration,
                }
            )
        quality.append({"model": _model_id(identity), "checks": checks})

    document = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration": total_duration,
        "status": status.value,
        "seeders": [
            {
                "name": name,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "status": result.status.value,
                "duration": result.total_duration,
                "execution_duration": result.execution_duration,
                "error": result.error,
            }
            for name, result in seeder_results.items()
        ],
        "models": [
            {
                **_model_id(identity),
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "status": result.status.value,
                "duration": result.duration,
                "materialization_duration": result.materialization_duration,
                "error": result.error,
            }
            for identity, result in dataset_results
        ],
        "lineage": [
            *(
                {
                    "job_type": "SEEDER",
                    "job_name": name,
                    "inputs": [],
                    "outputs": [
                        _model_id(identity) for identity in result.output_models
                    ],
                }
                for name, result in seeder_results.items()
            ),
            *(
                {
                    "job_type": "MODEL",
                    "job_name": str(model.id),
                    "inputs": [
                        _model_id(identity) for identity in sorted(model.dependencies)
                    ],
                    "outputs": [_model_id(model.id)],
                }
                for model in dag.models.values()
            ),
        ],
        "quality": quality,
    }
    line = json.dumps(document, default=_json_value, separators=(",", ":")) + "\n"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("a", encoding="utf-8") as state_file:
        state_file.write(line)
