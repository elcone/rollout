import logging

from rollout.dag import DAG
from rollout.models import ModelId

logger = logging.getLogger(__name__)


def _names(nodes: frozenset[ModelId]) -> str:
    return ", ".join(f"`{node}`" for node in sorted(nodes)) or "None"


def _mermaid_label(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def render_markdown(dag: DAG) -> str:
    logger.info("Rendering Markdown and Mermaid graph")

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
    lines.extend(f"{index}. `{node}`" for index, node in enumerate(dag.order, 1))

    if not dag.order:
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
