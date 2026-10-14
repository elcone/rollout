import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated

import duckdb
import typer

from rollout.config import EngineConfig
from rollout.dag import build_dag
from rollout.discovery import discover_models, discover_seeders
from rollout.execution import ExecutionStatus, connect, execute_models_with_connection
from rollout.parser import parse_models
from rollout.renderer import render_execution_markdown, render_markdown
from rollout.seeders import execute_seeder

logger = logging.getLogger(__name__)
app = typer.Typer(
    help="Inspect Model DAGs and execute project Seeders and Models in DuckDB."
)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show Seeder and Model debug messages."),
    ] = False,
) -> None:
    package_logger = logging.getLogger("rollout")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    previous_level = package_logger.level
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    def cleanup_logging() -> None:
        package_logger.removeHandler(handler)
        package_logger.setLevel(previous_level)
        handler.close()

    ctx.call_on_close(cleanup_logging)


def print_section(title: str) -> None:
    typer.echo()
    typer.secho(
        title,
        bold=True,
        fg=typer.colors.BRIGHT_WHITE,
    )


@app.command()
def dag(
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            help="Project directory containing models/ and optional seeders/.",
        ),
    ] = Path("."),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Save Markdown to a file instead of stdout."
        ),
    ] = None,
) -> None:
    try:
        models = discover_models(project_dir / "models")
        models = parse_models(models)
        dag = build_dag(models)
        markdown = render_markdown(dag)

        if output is None:
            logger.info("Writing Markdown to stdout")
            typer.echo(markdown, nl=False)
        else:
            output.write_text(markdown, encoding="utf-8")
            logger.info("Wrote Markdown to %s", output)

    except (ValueError, OSError) as exc:
        logger.error("Error: %s", exc)
        raise typer.Exit(code=1) from exc


@app.command()
def run(
    project_dir: Annotated[
        Path,
        typer.Option(
            "--project-dir",
            help="Project directory containing models/ and optional seeders/.",
        ),
    ] = Path("."),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Save the execution report here instead of .rollout/runs/.",
        ),
    ] = None,
) -> None:
    try:
        started_at = datetime.now(UTC)
        run_id = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
        engine_config = EngineConfig()
        started = perf_counter()
        seeder_results = {}
        with connect(engine_config) as connection:
            seeders_dir = project_dir / "seeders"
            seeders = discover_seeders(seeders_dir) if seeders_dir.exists() else []
            if not seeders_dir.exists():
                logger.info(
                    "No seeders directory at %s; executing zero seeders", seeders_dir
                )
            for seeder in seeders:
                name = seeder.path.relative_to(project_dir).as_posix()
                seeder_results[name] = execute_seeder(seeder, connection)
            models = discover_models(project_dir / "models")
            models = parse_models(models)
            dag = build_dag(models)
            results = execute_models_with_connection(
                connection, dag, engine_config, run_id
            )
        finished_at = datetime.now(UTC)
        total_duration = perf_counter() - started

    except (ValueError, OSError, duckdb.Error) as exc:
        logger.error("Error: %s", exc)
        raise typer.Exit(code=1) from exc

    symbols = {
        ExecutionStatus.SUCCESS: "🟢",
        ExecutionStatus.FAILED: "🔴",
        ExecutionStatus.SKIPPED: "🟡",
    }
    colors = {
        ExecutionStatus.SUCCESS: typer.colors.GREEN,
        ExecutionStatus.FAILED: typer.colors.RED,
        ExecutionStatus.SKIPPED: typer.colors.YELLOW,
    }
    seeder_outputs = {
        identity: result
        for seeder_result in seeder_results.values()
        for identity, result in seeder_result.outputs.items()
    }
    for label, category in (
        ("Seeders", seeder_results),
        ("Seeder outputs", seeder_outputs),
        ("Models", results),
    ):
        print_section(label)
        for identity, result in category.items():
            typer.secho(
                f"{symbols[result.status]} {identity}", fg=colors[result.status]
            )

    all_results = [
        *seeder_results.values(),
        *seeder_outputs.values(),
        *results.values(),
    ]

    typer.echo()
    for status, label in (
        (ExecutionStatus.SUCCESS, "successful"),
        (ExecutionStatus.FAILED, "failed"),
        (ExecutionStatus.SKIPPED, "skipped"),
    ):
        count = sum(result.status is status for result in all_results)
        typer.secho(f"{count} {label}", fg=colors[status])

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
        if output is not None:
            raise typer.Exit(code=1) from exc

    if any(result.status is ExecutionStatus.FAILED for result in all_results):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
