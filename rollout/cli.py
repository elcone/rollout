import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from rollout.dag import build_dag
from rollout.discovery import discover_models
from rollout.parser import parse_models
from rollout.renderer import render_markdown

logger = logging.getLogger(__name__)
app = typer.Typer(help="Discover SQL models and inspect their dependency DAG.")


@app.callback()
def main(
    ctx: typer.Context,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show per-model debug messages.")
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
) -> None:
    try:
        models = discover_models(models_dir)
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


if __name__ == "__main__":
    app()
