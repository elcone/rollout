import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from rollout.config import EngineConfig
from rollout.dag import build_dag
from rollout.discovery import discover_models, discover_seeders
from rollout.execution import (
    ExecutionStatus,
    ModelResult,
    connect,
    execute_models_with_connection,
)
from rollout.models import ModelId
from rollout.parser import parse_models
from rollout.renderer import render_execution_markdown
from rollout.seeders import SeederResult, execute_seeder
from rollout.state import persist_run_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: ExecutionStatus
    skip_seeders: bool
    started_at: datetime
    finished_at: datetime
    total_duration: float
    seeder_results: dict[str, SeederResult]
    model_results: dict[ModelId, ModelResult]
    report_path: Path
    report_error: str | None


def run_project(
    project_dir: str | Path = Path("."),
    *,
    skip_seeders: bool = False,
    output: str | Path | None = None,
) -> RunResult:
    """Execute a project and save its report and history.

    Paths retain their CLI semantics: the warehouse is relative to the working
    directory, while model and seeder discovery is relative to project_dir.
    Dataset failures are returned; setup and project validation errors propagate.
    Report write failures are returned separately from the execution status.
    """
    project_dir = Path(project_dir)
    output = Path(output) if output is not None else None
    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
    engine_config = EngineConfig()
    started = perf_counter()
    seeder_results = {}
    with connect(engine_config) as connection:
        logger.info("Seeder mode: %s", "skipped" if skip_seeders else "enabled")
        if not skip_seeders:
            seeders_dir = project_dir / "seeders"
            seeders = discover_seeders(seeders_dir) if seeders_dir.exists() else []
            if not seeders_dir.exists():
                logger.info(
                    "No seeders directory at %s; executing zero seeders", seeders_dir
                )
            for seeder in seeders:
                name = seeder.path.relative_to(project_dir).as_posix()
                seeder_results[name] = execute_seeder(seeder, connection, project_dir)
        models = discover_models(project_dir / "models")
        models = parse_models(models)
        dag = build_dag(models)
        results = execute_models_with_connection(connection, dag, engine_config, run_id)
    finished_at = datetime.now(UTC)
    total_duration = perf_counter() - started

    all_results = [
        *seeder_results.values(),
        *(
            result
            for seeder in seeder_results.values()
            for result in seeder.output_models.values()
        ),
        *results.values(),
    ]
    status = (
        ExecutionStatus.FAILED
        if any(result.status is ExecutionStatus.FAILED for result in all_results)
        else ExecutionStatus.SUCCESS
    )

    report_path = output
    if report_path is None:
        filename = f"{run_id}.md"
        report_path = engine_config.warehouse_path.parent / "runs" / filename
    markdown = render_execution_markdown(
        dag,
        results,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        total_duration=total_duration,
        seeder_results=seeder_results,
        skip_seeders=skip_seeders,
    )
    try:
        if output is None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("x", encoding="utf-8") as report_file:
                report_file.write(markdown)
        else:
            report_path.write_text(markdown, encoding="utf-8")
        logger.info("Wrote execution report to %s", report_path)
    except OSError as exc:
        logger.warning("Could not save execution report to %s: %s", report_path, exc)
        report_error = str(exc)
    else:
        report_error = None

    state_path = engine_config.warehouse_path.parent / "state.jsonl"
    try:
        persist_run_state(
            state_path,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            total_duration=total_duration,
            dag=dag,
            results=results,
            seeder_results=seeder_results,
            skip_seeders=skip_seeders,
        )
    except OSError as exc:
        logger.warning("Could not persist run state to %s: %s", state_path, exc)

    return RunResult(
        run_id=run_id,
        status=status,
        skip_seeders=skip_seeders,
        started_at=started_at,
        finished_at=finished_at,
        total_duration=total_duration,
        seeder_results=seeder_results,
        model_results=results,
        report_path=report_path,
        report_error=report_error,
    )
