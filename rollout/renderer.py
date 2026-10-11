import logging
from datetime import UTC, datetime
from html import escape

from rollout.dag import DAG
from rollout.execution import ExecutionStatus, ModelResult
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
    lines.extend(_mermaid_lines(dag))

    lines.extend(["```", ""])

    return "\n".join(lines)


def _mermaid_ids(dag: DAG) -> dict[ModelId, str]:
    return {node: f"n{index}" for index, node in enumerate(sorted(dag.dependencies))}


def _mermaid_lines(
    dag: DAG, results: dict[ModelId, ModelResult] | None = None
) -> list[str]:
    lines: list[str] = []
    ids = _mermaid_ids(dag)
    schemas: dict[str, list[ModelId]] = {}

    for node in ids:
        schemas.setdefault(node.schema, []).append(node)

    for index, (schema, nodes) in enumerate(sorted(schemas.items())):
        lines.append(f'    subgraph s{index}["{_mermaid_label(schema)}"]')
        for node in nodes:
            label = _mermaid_label(node.name)
            if results is not None and node in dag.internal_nodes:
                result = results[node]
                status = result.status.value
                if result.duration is not None:
                    status += f" · {result.duration:.2f}s"
                label += f"<br/>{status}"
            lines.append(f'        {ids[node]}("{label}")')
        lines.append("    end")

    for index in range(len(schemas)):
        lines.append(
            f"    style s{index} fill:#f3f4f6,stroke:#d1d5db,color:#374151,rx:10,ry:10"
        )

    for dependency, model in sorted(dag.edges):
        lines.append(f"    {ids[dependency]} --> {ids[model]}")

    if dag.external_nodes:
        lines.append("    classDef external stroke-dasharray: 5 5")
        lines.append(
            f"    class {','.join(ids[node] for node in sorted(dag.external_nodes))} external"
        )

    return lines


def _table_cell(value: str) -> str:
    return (
        escape(value, quote=False)
        .replace("|", "&#124;")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def render_execution_markdown(
    dag: DAG,
    results: dict[ModelId, ModelResult],
    *,
    generated_at: datetime | None = None,
) -> str:
    if generated_at is None:
        generated_at = datetime.now(UTC)
    timestamp = generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    failed = [
        node for node in dag.order if results[node].status is ExecutionStatus.FAILED
    ]
    lines = [
        "# Rollout execution",
        "",
        f"Generated at: {timestamp}",
        "",
        "## Summary",
        "",
    ]
    for status, label in (
        (ExecutionStatus.SUCCESS, "Successful"),
        (ExecutionStatus.FAILED, "Failed"),
        (ExecutionStatus.SKIPPED, "Skipped"),
    ):
        count = sum(results[node].status is status for node in dag.order)
        lines.append(f"- {label}: {count}")

    lines.extend(["", "## Root failures", ""])
    lines.extend(f"- {node}" for node in failed)
    if not failed:
        lines.append("None")

    lines.extend(
        [
            "",
            "## Model results",
            "",
            "| Model | Status | Duration | Error |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for node in dag.order:
        result = results[node]
        duration = f"{result.duration:.2f}s" if result.duration is not None else "—"
        error = "See failure details" if result.status is ExecutionStatus.FAILED else ""
        lines.append(
            f"| {_table_cell(str(node))} | {result.status.value} | {duration} | {error} |"
        )

    lines.extend(["", "## Failure details", ""])
    if not failed:
        lines.append("None")
    for node in failed:
        lines.extend([f"### {node}", ""])
        # Indented code preserves multiline DuckDB diagnostics verbatim.
        error = results[node].error or "No error message available."
        lines.extend(f"    {line}" for line in error.splitlines())
        lines.append("")

    lines.extend(
        [
            "",
            "## Execution lineage",
            "",
            "Green: SUCCESS · Red: FAILED · Yellow: SKIPPED · Dashed: external dependency",
            "",
            "```mermaid",
            "flowchart LR",
        ]
    )
    lines.extend(_mermaid_lines(dag, results))
    lines.extend(
        [
            "    classDef SUCCESS fill:#dcfce7,stroke:#15803d,color:#14532d",
            "    classDef FAILED fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
            "    classDef SKIPPED fill:#fef9c3,stroke:#a16207,color:#713f12",
        ]
    )
    ids = _mermaid_ids(dag)
    for node in dag.order:
        lines.append(f"    class {ids[node]} {results[node].status.value}")
    lines.extend(["```", ""])
    return "\n".join(lines)
