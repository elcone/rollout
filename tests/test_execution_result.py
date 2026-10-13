from datetime import UTC, datetime
import re

from rollout.execution import ExecutionStatus, ModelResult
from rollout.renderer import render_execution_markdown


RUN_TIMING = {
    "started_at": datetime(2026, 10, 11, 23, 13, 24, tzinfo=UTC),
    "finished_at": datetime(2026, 10, 11, 23, 13, 29, tzinfo=UTC),
    "total_duration": 5.42,
}

def test_mixed_results_order_summary_and_lineage(model_dag):
    dag = model_dag(
        {
            "bronze/orders.sql": "SELECT * FROM external.orders",
            "silver/orders.sql": "SELECT * FROM bronze.orders",
            "gold/sales.sql": "SELECT * FROM silver.orders",
        }
    )
    success, failed, skipped = dag.order
    # Mapping insertion order must not determine report order.
    results = {
        skipped: ModelResult(ExecutionStatus.SKIPPED),
        failed: ModelResult(ExecutionStatus.FAILED, "Binder Error: missing", 0.084),
        success: ModelResult(ExecutionStatus.SUCCESS, duration=0.126),
    }
    report = render_execution_markdown(dag, results, **RUN_TIMING)
    assert "- Successful: 1\n- Failed: 1\n- Skipped: 1" in report
    rows = [line for line in report.split("## Model results", 1)[1].split("## Quality details", 1)[0].splitlines() if line.startswith("| ")][2:]
    assert rows == [
        "| bronze.orders | SUCCESS | 0.13s | — |  |",
        "| silver.orders | FAILED | 0.08s | — | See failure details |",
        "| gold.sales | SKIPPED | — | — |  |",
    ]
    groups = dict(re.findall(r'subgraph \w+\["([^"]+)"\]\n(.*?)    end', report, re.S))
    ids = {
        schema: re.search(r'(n\d+)\("', content).group(1)
        for schema, content in groups.items()
    }
    for index in range(len(groups)):
        assert f"style s{index} fill:#f3f4f6,stroke:#d1d5db,color:#374151" in report
    for schema, status in (
        ("bronze", "SUCCESS"),
        ("silver", "FAILED"),
        ("gold", "SKIPPED"),
    ):
        assert f"classDef {status} fill:" in report
        assert f"class {ids[schema]} {status}" in report
    assert f'{ids["bronze"]}("orders<br/>SUCCESS · 0.13s")' in report
    assert f'{ids["silver"]}("orders<br/>FAILED · 0.08s")' in report
    assert f'{ids["gold"]}("sales<br/>SKIPPED")' in report
    assert f'{ids["external"]}("orders")' in report
    assert "classDef external stroke-dasharray: 5 5" in report
    assert f"class {ids['external']} external" in report
    for upstream, downstream in (
        ("external", "bronze"),
        ("bronze", "silver"),
        ("silver", "gold"),
    ):
        assert f"{ids[upstream]} --> {ids[downstream]}" in report


def test_error_preserves_markdown_table(model_dag):
    dag = model_dag({"orders.sql": "SELECT 1"})
    report = render_execution_markdown(
        dag,
        {
            dag.order[0]: ModelResult(
                ExecutionStatus.FAILED, "Error | <value>\r\nnext line", 0.0
            ),
        },
        **RUN_TIMING,
    )
    assert "| FAILED | 0.00s | — | See failure details |" in report
    assert "    Error | <value>\n    next line" in report


def test_empty_execution_report(model_dag):
    report = render_execution_markdown(model_dag({}), {}, **RUN_TIMING)
    assert "- Successful: 0\n- Failed: 0\n- Skipped: 0" in report
    assert "```mermaid\nflowchart LR" in report
    assert "subgraph" not in report


def test_execution_node_escapes_name_before_adding_status(model_dag):
    dag = model_dag({"a&b.sql": "SELECT 1"})
    report = render_execution_markdown(
        dag,
        {
            dag.order[0]: ModelResult(ExecutionStatus.SUCCESS, duration=0.14),
        },
        **RUN_TIMING,
    )
    assert '("a&amp;b<br/>SUCCESS · 0.14s")' in report


def test_run_timing_and_section_order(model_dag):
    from datetime import timedelta, timezone

    dag = model_dag({"orders.sql": "SELECT 1"})
    timestamp = datetime(2026, 10, 11, 3, 3, 32, tzinfo=timezone(timedelta(hours=3)))
    report = render_execution_markdown(
        dag,
        {
            dag.order[0]: ModelResult(ExecutionStatus.SUCCESS, duration=0.14),
        },
        started_at=timestamp,
        finished_at=timestamp + timedelta(seconds=5),
        total_duration=5.42,
    )
    assert "Started at: 2026-10-11 00:03:32 UTC" in report
    assert "Finished at: 2026-10-11 00:03:37 UTC" in report
    assert "Duration: 5.42s" in report
    assert "Generated at" not in report
    assert "| SUCCESS | 0.14s |" in report
    assert "## Root failures\n\nNone" in report
    assert "## Failure details\n\nNone" in report
    sections = [
        "# Rollout execution",
        "Started at:",
        "Finished at:",
        "Duration:",
        "## Summary",
        "## Root failures",
        "## Model results",
        "## Failure details",
        "## Execution lineage",
    ]
    assert [report.index(section) for section in sections] == sorted(
        report.index(section) for section in sections
    )


def test_root_failures_and_details_follow_execution_order(model_dag, engine_config):
    from rollout.execution import execute_models

    dag = model_dag(
        {
            "bronze/orders.sql": "SELECT nonexistent_column FROM (SELECT 1 AS id)",
            "silver/orders.sql": "SELECT * FROM bronze.orders",
            "other/orders.sql": "SELECT CAST('invalid' AS INTEGER) AS id",
        }
    )
    results = execute_models(dag, engine_config)
    report = render_execution_markdown(
        dag, dict(reversed(list(results.items()))), **RUN_TIMING
    )
    failed = [
        node for node in dag.order if results[node].status is ExecutionStatus.FAILED
    ]
    root_section = report.split("## Root failures\n\n", 1)[1].split(
        "## Seeder results", 1
    )[0]
    assert root_section.strip().splitlines() == [f"- {node}" for node in failed]
    details = report.split("## Failure details", 1)[1].split("## Execution lineage", 1)[
        0
    ]
    assert [details.index(f"### {node}") for node in failed] == sorted(
        details.index(f"### {node}") for node in failed
    )
    for node in failed:
        formatted_error = "\n".join(
            f"    {line}" for line in results[node].error.splitlines()
        )
        assert formatted_error in details
        assert report.count(formatted_error) == 1
    assert "LINE" in details
    assert "^" in details
    assert "### silver.orders" not in details
