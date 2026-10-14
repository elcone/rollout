import logging
from datetime import UTC, datetime
from html import escape

from jinja2 import Environment, PackageLoader

from rollout.config import (
    CustomSqlCheck,
    ExpectationOperator,
    NullnessCheck,
    RowCountCheck,
    UniquenessCheck,
)
from rollout.dag import DAG
from rollout.execution import ExecutionStatus, ModelResult
from rollout.models import ModelId
from rollout.quality import CheckStatus
from rollout.seeders import SeederResult

_EXPECTATION_SYMBOLS = {
    ExpectationOperator.GT: ">",
    ExpectationOperator.GTE: ">=",
    ExpectationOperator.EQ: "=",
    ExpectationOperator.NEQ: "<>",
    ExpectationOperator.LT: "<",
    ExpectationOperator.LTE: "<=",
}

logger = logging.getLogger(__name__)
_templates = Environment(
    loader=PackageLoader("rollout", "templates/markdown"),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def _quality_summary(result: ModelResult) -> str:
    counts = {"PASS": 0, "WARNING": 0, "ERROR": 0}
    for check in result.quality_results:
        label = "PASS" if check.status is CheckStatus.PASS else check.severity.value
        counts[label] += 1
    return (
        " · ".join(f"{count} {label}" for label, count in counts.items() if count)
        or "—"
    )


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

    models = [
        {
            "name": str(node),
            "dependencies": _names(dag.dependencies[node]),
            "dependents": _names(dag.dependents[node]),
            "external_dependencies": _names(
                dag.dependencies[node] & dag.external_nodes
            ),
        }
        for node in sorted(dag.models)
    ]
    return _templates.get_template("dag.md.j2").render(
        model_count=len(dag.models),
        dependency_count=len(dag.edges),
        external_count=len(dag.external_nodes),
        execution_order=[str(node) for node in dag.order],
        models=models,
        mermaid="\n".join(["flowchart LR", *_mermaid_lines(dag)]),
    )


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
                if (
                    result.quality_results
                    and result.status is not ExecutionStatus.SKIPPED
                ):
                    label += f"<br/>Quality: {_quality_summary(result)}"
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
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    total_duration: float,
    seeder_results: dict[str, SeederResult] | None = None,
) -> str:
    seeder_results = seeder_results or {}
    output_results = {
        node: result
        for seeder in seeder_results.values()
        for node, result in seeder.outputs.items()
    }
    dataset_results = {**output_results, **{node: results[node] for node in dag.order}}
    all_results = [*seeder_results.values(), *dataset_results.values()]
    summary = [
        (label, sum(result.status is status for result in all_results))
        for status, label in (
            (ExecutionStatus.SUCCESS, "Successful"),
            (ExecutionStatus.FAILED, "Failed"),
            (ExecutionStatus.SKIPPED, "Skipped"),
        )
    ]
    dataset_rows = {}
    for node, result in dataset_results.items():
        dataset_rows[node] = {
            "name": _table_cell(str(node)),
            "status": result.status.value,
            "quality": _quality_summary(result),
            "duration": f"{result.duration:.2f}s"
            if result.duration is not None
            else "—",
            "materialization_duration": f"{result.materialization_duration:.2f}s"
            if result.materialization_duration is not None
            else "—",
            "error": "See failure details"
            if result.status is ExecutionStatus.FAILED
            else "",
        }
    quality_details = []
    for node, dataset_result in dataset_results.items():
        for result in dataset_result.quality_results:
            check = result.check
            if isinstance(check, CustomSqlCheck):
                name = check.name
            elif isinstance(check, NullnessCheck):
                name = f"nullness({check.column})"
            elif isinstance(check, UniquenessCheck):
                name = f"uniqueness({', '.join(check.columns)})"
            elif isinstance(check, RowCountCheck):
                name = "row_count"
            else:
                raise ValueError(f"Unsupported quality check: {type(check).__name__}")
            if isinstance(check, CustomSqlCheck):
                expected = " AND ".join(
                    f"{_EXPECTATION_SYMBOLS[item.operator]} "
                    f"{str(item.value).removesuffix('.0')}"
                    for item in check.expectations
                )
            elif isinstance(check, RowCountCheck):
                expected = f">= {check.min}"
            elif isinstance(check, NullnessCheck):
                expected = f"<= {check.max}"
            elif isinstance(check, UniquenessCheck):
                expected = f"<= {check.max}"
            else:
                raise ValueError(f"Unsupported quality check: {type(check).__name__}")
            quality_details.append(
                {
                    "model": _table_cell(str(node)),
                    "check": _table_cell(name),
                    "observed": str(result.observed_value),
                    "expected": expected,
                    "status": result.status.value,
                    "severity": result.severity.value,
                    "duration": f"{result.duration:.2f}s",
                }
            )
    failures = []
    for node, result in dataset_results.items():
        if result.status is not ExecutionStatus.FAILED:
            continue
        error = result.error or "No error message available."
        failures.append(
            {
                "name": str(node),
                "error": "\n".join(f"    {line}" for line in error.splitlines()),
            }
        )

    for name, result in seeder_results.items():
        if result.error is not None:
            failures.append(
                {
                    "name": f"Seeder {_table_cell(name)} failed before outputs",
                    "error": "\n".join(
                        f"    {line}" for line in result.error.splitlines()
                    ),
                }
            )

    seeder_rows = [
        {
            "name": _table_cell(name),
            "status": result.status.value,
            "execution_duration": f"{result.execution_duration:.2f}s"
            if result.execution_duration is not None
            else "—",
            "total_duration": f"{result.total_duration:.2f}s",
            "error": _table_cell(result.error) if result.error else "",
        }
        for name, result in seeder_results.items()
    ]
    output_rows = [dataset_rows[node] for node in output_results]
    models = [dataset_rows[node] for node in dag.order]

    mermaid_lines = ["flowchart LR", *_mermaid_lines(dag, results)]
    mermaid_lines.extend(
        [
            "    classDef SUCCESS fill:#dcfce7,stroke:#15803d,color:#14532d",
            "    classDef FAILED fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d",
            "    classDef SKIPPED fill:#fef9c3,stroke:#a16207,color:#713f12",
        ]
    )
    ids = _mermaid_ids(dag)
    for node in dag.order:
        mermaid_lines.append(f"    class {ids[node]} {results[node].status.value}")

    return _templates.get_template("execution.md.j2").render(
        run_id=run_id,
        started_at=started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        finished_at=finished_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        total_duration=f"{total_duration:.2f}s",
        summary=summary,
        models=models,
        seeders=seeder_rows,
        seeder_outputs=output_rows,
        failures=failures,
        quality_details=quality_details,
        mermaid="\n".join(mermaid_lines),
    )
