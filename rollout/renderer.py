"""Markdown reports and Mermaid diagrams for SQL model dependencies."""

from rollout.dag import DAG, topological_sort
from rollout.logging import get_logger
from rollout.models import ModelId

logger = get_logger(__name__)


def _names(nodes: frozenset[ModelId]) -> str:
    """Format model identities as a sorted Markdown list.

    Args:
        nodes: Identities to format.

    Returns:
        Comma-separated inline code labels, or None as text for an empty set.
    """
    return ", ".join(f"`{node}`" for node in sorted(nodes)) or "None"


def _mermaid_label(value: str) -> str:
    """Escape text for a quoted Mermaid label.

    Args:
        value: Raw schema or model label.

    Returns:
        Text with HTML-sensitive characters escaped and line breaks removed.
    """
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def render_markdown(dag: DAG, order: list[ModelId] | None = None) -> str:
    """Render a dependency report and a Mermaid flowchart.

    The graph groups nodes by schema, shows model names in rounded nodes,
    and marks external sources with dashed outlines. Text sections retain
    qualified identities. This function does not write files.

    Args:
        dag: Dependency graph to describe.
        order: Precomputed local model order. If None, compute and validate it.
            Supplied orders are used as provided, without validation.

    Returns:
        Markdown with counts, model order, dependencies, and a Mermaid graph.

    Raises:
        ValueError: The graph contains a cycle when order is not supplied.
    """
    logger.info("Rendering Markdown and Mermaid graph")

    if order is None:
        order = topological_sort(dag)

    lines = [
        "# Rollout DAG",
        "",
        "## Project summary",
        "",
        f"- Models: {len(dag.models)}",
        f"- Dependency relationships: {len(dag.edges)}",
        f"- External dependencies: {len(dag.external_nodes)}",
        "",
        "## Execution order",
        "",
    ]
    lines.extend(f"{index}. `{node}`" for index, node in enumerate(order, 1))

    if not order:
        lines.append("No models.")

    lines.extend(["", "## Model dependencies", ""])

    for node in sorted(dag.models):
        lines.extend(
            [
                f"### `{node}`",
                "",
                f"- Dependencies: {_names(dag.dependencies[node])}",
                f"- Dependents: {_names(dag.dependents[node])}",
                f"- External dependencies: {_names(dag.dependencies[node] & dag.external_nodes)}",
                "",
            ]
        )

    lines.extend(["## Mermaid graph", "", "```mermaid", "flowchart LR"])
    ids = {node: f"n{index}" for index, node in enumerate(sorted(dag.dependencies))}
    schemas: dict[str, list[ModelId]] = {}

    for node in ids:
        schemas.setdefault(node.schema, []).append(node)

    for index, (schema, nodes) in enumerate(sorted(schemas.items())):
        lines.append(f'    subgraph s{index}["{_mermaid_label(schema)}"]')
        for node in nodes:
            lines.append(f'        {ids[node]}("{_mermaid_label(node.name)}")')
        lines.append("    end")

    for index in range(len(schemas)):
        lines.append(f"    style s{index} rx:10,ry:10")

    for dependency, model in sorted(dag.edges):
        lines.append(f"    {ids[dependency]} --> {ids[model]}")

    if dag.external_nodes:
        lines.append("    classDef external stroke-dasharray: 5 5")
        lines.append(
            f"    class {','.join(ids[node] for node in sorted(dag.external_nodes))} external"
        )

    lines.extend(["```", ""])

    return "\n".join(lines)
