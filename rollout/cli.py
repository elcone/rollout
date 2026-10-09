"""Terminal interface to the public Rollout API."""

from pathlib import Path
from typing import Annotated

import typer

from rollout import Rollout
from rollout.context import execution_context, new_execution_id
from rollout.logging import cli_logging, get_logger

logger = get_logger("rollout.cli")
app = typer.Typer(help="Discover SQL models and inspect their dependency DAG.")


@app.callback()
def main() -> None:
    """Initialize the CLI command group without performing an operation."""


@app.command()
def dag(
    models_dir: Annotated[
        Path, typer.Option("--models-dir", help="Directory containing SQL models.")
    ] = Path("models"),
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Save Markdown to a file instead of stdout."
        ),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show per-model debug messages.")
    ] = False,
) -> None:
    """Validate SQL model dependencies and export a Markdown DAG.

    Progress and errors are logged to stderr with a shared execution ID.

    Args:
        models_dir: Directory containing SQL model files.
        output: Destination file, or None to write Markdown to stdout.
        verbose: Whether to include per-model DEBUG messages.

    Raises:
        typer.Exit: Model validation or file I/O fails; the exit code is 1.
    """
    identifier = new_execution_id()
    with cli_logging(verbose), execution_context(identifier, "dag"):
        try:
            result = Rollout(models_dir).dag(execution_id=identifier)

            if output is None:
                logger.info("Writing Markdown to stdout")
                typer.echo(result.markdown, nl=False)

            else:
                output.write_text(result.markdown, encoding="utf-8")
                logger.info("Wrote Markdown to %s", output)

        except (ValueError, OSError) as exc:
            logger.error("Error: %s", exc)
            raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
