"""Public Python interface shared with the CLI."""

from dataclasses import dataclass
from pathlib import Path

from rollout.context import execution_context, new_execution_id
from rollout.dag import DAG, build_dag, topological_sort
from rollout.discovery import discover_models
from rollout.logging import get_logger
from rollout.models import ModelId
from rollout.parser import parse_models
from rollout.renderer import render_markdown

logger = get_logger(__name__)


@dataclass(frozen=True)
class DAGResult:
    """Result of one successful DAG generation operation.

    Attributes:
        execution_id: Identifier used by the operation's log records.
        graph: Validated graph, including local models and external sources.
        order: Local model identities in dependency order.
        markdown: Generated document, including the Mermaid graph.
    """
    execution_id: str
    graph: DAG
    order: list[ModelId]
    markdown: str


class Rollout:
    """Coordinate static SQL model analysis for a filesystem project.

    Attributes:
        models_dir: Directory scanned for SQL models on each operation.
    """
    def __init__(self, models_dir: str | Path = "models") -> None:
        """Configure a project without reading or validating its files.

        Args:
            models_dir: Root directory containing SQL models.
        """
        self.models_dir = Path(models_dir)

    def dag(self, *, execution_id: str | None = None) -> DAGResult:
        """Discover models and generate a validated DAG document.

        SQL is analyzed but never executed. No output files are written. Each call
        uses a fresh execution context, restored even when the operation fails.

        Args:
            execution_id: Caller-provided ID, or None to generate a UTC timestamp
                with an English weekday and a random hexadecimal suffix.

        Returns:
            The execution ID, graph, local model order, and Markdown document.

        Raises:
            ValueError: The ID, project layout, SQL, or dependency graph is invalid.
            OSError: A model file cannot be read.
            UnicodeDecodeError: A model file is not valid UTF-8.
        """
        identifier = new_execution_id() if execution_id is None else execution_id

        with execution_context(identifier, "dag"):
            logger.info("Starting DAG generation")
            graph = build_dag(parse_models(discover_models(self.models_dir)))
            logger.info("Computing topological order")
            order = topological_sort(graph)
            document = render_markdown(graph, order)
            logger.info("DAG generation completed")

            return DAGResult(identifier, graph, order, document)
