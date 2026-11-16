from pathlib import Path

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from rollout.cli import app
from rollout.discovery import discover_seeders
from rollout.execution import ExecutionStatus
from rollout.models import ModelId
from rollout.seeders import execute_seeder


def write_seeder(project, name, body, outputs):
    path = project / "seeders" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def run(connection, staging_dir):\n"
        + "".join("    " + line + "\n" for line in body.splitlines())
    )
    path.with_suffix(".yaml").write_text(
        yaml.safe_dump({"outputs": outputs}, sort_keys=False)
    )


def test_project_seeder_before_model_and_shared_connection(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "models" / "silver").mkdir(parents=True)
    (project / "models" / "silver" / "orders.sql").write_text(
        "SELECT orders.id FROM staging.orders AS orders JOIN session_marker USING (id)"
    )
    write_seeder(
        project,
        "source.py",
        "connection.execute('CREATE TEMP TABLE session_marker AS SELECT 7 AS id')\n"
        "return {'orders': connection.sql('SELECT 7 AS id')}",
        {"orders": {}},
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--project-dir", str(project)])
    assert result.exit_code == 0, result.output
    assert "🟢 seeders/source.py" in result.stdout
    assert "🟢 staging.orders" in result.stdout
    assert "🟢 silver.orders" in result.stdout
    with duckdb.connect(str(tmp_path / ".rollout" / "warehouse.duckdb")) as connection:
        assert connection.sql("SELECT * FROM silver.orders").fetchall() == [(7,)]
    report = next((tmp_path / ".rollout" / "runs").glob("*.md")).read_text()
    assert (
        "| Dataset | Status | Duration | Materialization | Quality | Error |" in report
    )
    assert "| Model | Status | Duration | Materialization | Quality | Error |" in report
    assert (
        report.index("## Seeder results")
        < report.index("## Seeder outputs")
        < report.index("## Model results")
    )
    graph = report.split("```mermaid")[1]
    assert "source.py" not in graph
    assert "classDef external stroke-dasharray" in graph
    assert len(list((tmp_path / ".rollout" / "models").rglob("*.sql"))) == 1
    dag = CliRunner().invoke(app, ["dag", "--project-dir", str(project)])
    assert dag.exit_code == 0, dag.output
    assert "staging.orders" in dag.stdout


@pytest.mark.parametrize("failure", ["python", "quality", "warning"])
def test_failure_continuation_order_and_report(tmp_path, monkeypatch, failure):
    project = tmp_path / "project"
    (project / "models").mkdir(parents=True)
    (project / "models" / "report.sql").write_text("SELECT * FROM staging.later")
    marker = tmp_path / "order.txt"
    prefix = f"from pathlib import Path\np = Path({str(marker)!r})\np.write_text(p.read_text() + 'a' if p.exists() else 'a')\n"
    body = prefix + (
        "raise ValueError('source failed')"
        if failure == "python"
        else "return {'bad': connection.sql('SELECT 1 AS id'), 'good': connection.sql('SELECT 2 AS id')}"
    )
    outputs = {
        "bad": {
            "quality": [
                {
                    "type": "row_count",
                    "min": 2,
                    "severity": "WARNING" if failure == "warning" else "ERROR",
                }
            ]
        }
    }
    if failure != "python":
        outputs["good"] = {}
    write_seeder(project, "a.py", body, outputs)
    write_seeder(
        project,
        "z.py",
        f"from pathlib import Path\np = Path({str(marker)!r})\np.write_text(p.read_text() + 'z')\n"
        "return {'later': connection.sql('SELECT 3 AS id')}",
        {"later": {}},
    )
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "report.md"
    result = CliRunner().invoke(
        app, ["run", "--project-dir", str(project), "-o", str(output)]
    )
    assert result.exit_code == (0 if failure == "warning" else 1), result.output
    assert marker.read_text() == "az"
    assert "🟢 seeders/z.py" in result.stdout
    assert "🟢 public.report" in result.stdout
    report = output.read_text()
    for section in (
        "Seeder results",
        "Seeder outputs",
        "Model results",
        "Quality details",
        "Failure details",
    ):
        assert f"## {section}" in report
    if failure == "python":
        assert "failed before outputs" in report
        assert "source failed" in report
        assert "| staging.bad |" not in report
    else:
        assert "| staging.good | SUCCESS |" in report
        assert (
            "| staging.bad | SUCCESS |"
            if failure == "warning"
            else "| staging.bad | FAILED |"
        ) in report
        import re

        assert re.search(
            r"\| row_count \| 1 \| >= 2 \| FAIL \| (ERROR|WARNING) \| \d+\.\d{2}s \|",
            report,
        )
    with duckdb.connect(str(tmp_path / ".rollout" / "warehouse.duckdb")) as connection:
        assert connection.sql("SELECT * FROM public.report").fetchall() == [(3,)]


def test_models_required_and_old_option_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 1
    assert "Models directory does not exist" in result.stderr
    for command in ("run", "dag"):
        result = CliRunner().invoke(app, [command, "--models-dir", "."])
        assert result.exit_code != 0
        assert "No such option" in result.output


@pytest.mark.parametrize("fails", [False, True])
def test_seeder_timing_and_staging_retention(tmp_path, monkeypatch, fails):
    from rollout import seeders

    marker = tmp_path / "staging_path.txt"
    write_seeder(
        tmp_path,
        "source.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text(str(staging_dir))\n"
        + (
            "raise ValueError('timed failure')"
            if fails
            else "return {'orders': connection.sql('SELECT 1 AS id')}"
        ),
        {"orders": {}},
    )
    ticks = iter([10.0, 11.0, 14.0, 20.0])
    monkeypatch.setattr(seeders, "perf_counter", lambda: next(ticks))
    with duckdb.connect() as connection:
        result = execute_seeder(discover_seeders(tmp_path / "seeders")[0], connection, tmp_path)
    assert result.started_at.utcoffset().total_seconds() == 0
    assert result.finished_at.utcoffset().total_seconds() == 0
    assert result.finished_at >= result.started_at
    for output in result.output_models.values():
        assert (
            result.started_at
            <= output.started_at
            <= output.finished_at
            <= result.finished_at
        )
    assert result.execution_duration == 3.0
    assert result.total_duration == 10.0
    assert result.status is (
        ExecutionStatus.FAILED if fails else ExecutionStatus.SUCCESS
    )
    directory = Path(marker.read_text())
    assert directory.is_dir()
    assert directory == tmp_path / ".rollout" / "source"
    assert list(result.output_models) == (
        [] if fails else [ModelId("staging", "orders")]
    )


@pytest.mark.parametrize("skip_seeders", [False, True])
@pytest.mark.parametrize("failure", [None, "quality", "missing"])
def test_run_modes_staging_quality_and_history(tmp_path, monkeypatch, skip_seeders, failure):
    import json

    project = tmp_path / "project"
    models = project / "models"
    models.mkdir(parents=True)
    (models / "orders.sql").write_text("SELECT * FROM staging.orders")
    (models / "orders.yaml").write_text(
        yaml.safe_dump({"quality": [{"type": "row_count", "min": 2 if failure == "quality" else 1, "severity": "ERROR"}]})
    )
    (models / "report.sql").write_text("SELECT * FROM orders")
    (models / "independent.sql").write_text("SELECT 1 AS id")
    marker = project / "download.txt"
    write_seeder(
        project,
        "source.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('downloaded')\n"
        "return {'orders': connection.sql('SELECT 7 AS id')}",
        {"orders": {"quality": [{"type": "row_count", "min": 1, "severity": "ERROR"}]}},
    )
    monkeypatch.chdir(tmp_path)
    warehouse = tmp_path / ".rollout" / "warehouse.duckdb"
    warehouse.parent.mkdir()
    with duckdb.connect(str(warehouse)) as connection:
        connection.execute("CREATE SCHEMA staging")
        if failure != "missing":
            connection.execute("CREATE TABLE staging.orders AS SELECT 42 AS id")
    if skip_seeders:
        # Importing the module or running its output checks must both be avoided.
        seeder = project / "seeders" / "source.py"
        seeder.write_text("raise RuntimeError('seeder imported')\n" + seeder.read_text())
        seeder.with_suffix(".yaml").write_text(
            yaml.safe_dump({"outputs": {"orders": {"quality": [{"type": "row_count", "min": 999, "severity": "ERROR"}]}}})
        )
    args = ["run", "--project-dir", str(project)]
    if skip_seeders:
        args.append("--skip-seeders")
    result = CliRunner().invoke(app, args)
    fails = failure == "quality" or (skip_seeders and failure == "missing")
    assert result.exit_code == int(fails), result.output
    mode = "skipped" if skip_seeders else "enabled"
    assert f"Seeder mode: {mode}" in result.stderr
    assert marker.exists() is not skip_seeders
    assert "🟢 public.independent" in result.stdout
    if fails:
        assert "🔴 public.orders" in result.stdout
        assert "🟡 public.report" in result.stdout
    else:
        assert "🟢 public.orders" in result.stdout
        assert "🟢 public.report" in result.stdout
    event = json.loads((warehouse.parent / "state.jsonl").read_text())
    assert event["skip_seeders"] is skip_seeders
    assert event["status"] == ("FAILED" if fails else "SUCCESS")
    assert len(event["seeders"]) == (0 if skip_seeders else 1)
    assert any(job["job_type"] == "SEEDER" for job in event["lineage"]) is not skip_seeders
    groups = {group["model"]["schema"]: group for group in event["quality"]}
    assert ("staging" in groups) is not skip_seeders
    if failure != "missing" or not skip_seeders:
        assert groups["public"]["checks"][0]["status"] == ("FAIL" if fails else "PASS")
    else:
        assert not event["quality"]
        orders = next(model for model in event["models"] if model["name"] == "orders")
        assert "orders" in orders["error"]
    report = (warehouse.parent / "runs" / f"{event['run_id']}.md").read_text()
    assert f"Seeder mode: {mode}" in report
    if skip_seeders:
        assert "seeders/source.py" not in report
        assert all(model["schema"] != "staging" for model in event["models"])
        assert not (project / ".rollout").exists()
    with duckdb.connect(str(warehouse)) as connection:
        if skip_seeders and failure == "missing":
            assert connection.sql("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'staging'").fetchone() == (0,)
        else:
            assert connection.sql("SELECT * FROM staging.orders").fetchall() == [(42 if skip_seeders else 7,)]


def test_skip_seeders_does_not_discover_invalid_configuration(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "numbers.sql").write_text("SELECT 1")
    (tmp_path / "seeders").mkdir()
    (tmp_path / "seeders" / "source.py").write_text("raise RuntimeError('imported')")
    (tmp_path / "seeders" / "source.yaml").write_text("outputs: [invalid")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["run", "--skip-seeders"])
    assert result.exit_code == 0, result.output
    normal = runner.invoke(app, ["run"])
    assert normal.exit_code == 1
    assert "Invalid seeder configuration" in normal.stderr
