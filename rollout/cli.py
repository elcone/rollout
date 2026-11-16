import logging
import sys
from pathlib import Path
from typing import Annotated

import duckdb
import typer

from rollout.dag import build_dag
from rollout.discovery import discover_models
from rollout.execution import ExecutionStatus
from rollout.parser import parse_models
from rollout.api import run_project
from rollout.renderer import render_markdown

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
def viewer() -> None:
    """Explore the current project's DAG and column lineage in a browser."""
    import uvicorn

    uvicorn.run("tools.viewer.app:app", host="127.0.0.1", port=8000)


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
    skip_seeders: Annotated[
        bool,
        typer.Option(
            "--skip-seeders",
            help="Use existing staging without running seeders or downloads.",
        ),
    ] = False,
) -> None:
    try:
        run_result = run_project(project_dir, skip_seeders=skip_seeders, output=output)
        seeder_results = run_result.seeder_results
        results = run_result.model_results

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
        for identity, result in seeder_result.output_models.items()
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

    if output is not None and run_result.report_error is not None:
        raise typer.Exit(code=1)

    if run_result.status is ExecutionStatus.FAILED:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
