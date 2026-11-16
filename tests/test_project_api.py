import json
import logging
import re
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from rollout import RunResult, run_project
from rollout.cli import app
from rollout.execution import ExecutionStatus
from rollout.models import ModelId


def write_project(root, scenario):
    models = root / "models"
    models.mkdir(parents=True)
    (models / "orders.sql").write_text("SELECT * FROM staging.orders")
    (models / "report.sql").write_text("SELECT * FROM orders")
    (models / "independent.sql").write_text("SELECT 1 AS id")
    severity = "WARNING" if scenario == "warning" else "ERROR"
    minimum = 2 if scenario in ("quality", "warning") else 1
    (models / "orders.yaml").write_text(
        f"quality:\n  - type: row_count\n    min: {minimum}\n    severity: {severity}\n"
    )
    seeders = root / "seeders"
    seeders.mkdir()
    (seeders / "source.py").write_text(
        "def run(connection, staging_dir):\n"
        "    (staging_dir / 'download.txt').write_text('downloaded')\n"
        + (
            "    raise ValueError('source failed')\n"
            if scenario == "seeder"
            else "    return {'orders': connection.sql('SELECT 7 AS id')}\n"
        )
    )
    (seeders / "source.yaml").write_text(
        "outputs:\n  orders:\n    quality:\n      - type: row_count\n"
        "        min: 1\n        severity: ERROR\n"
    )


def normalize_document(value):
    if isinstance(value, dict):
        return {
            key: normalize_document(item)
            for key, item in value.items()
            if key
            not in {
                "run_id",
                "started_at",
                "finished_at",
                "duration",
                "execution_duration",
                "materialization_duration",
            }
        }
    if isinstance(value, list):
        return [normalize_document(item) for item in value]
    return value


def normalize_report(report):
    report = re.sub(
        r"^(Run ID|Started at|Finished at|Duration): .*", "", report, flags=re.M
    )
    return re.sub(r"\d+\.\d{2}s", "<duration>", report)


@pytest.mark.parametrize("skip_seeders", [False, True])
@pytest.mark.parametrize(
    "scenario", ["success", "missing", "quality", "warning", "seeder"]
)
def test_cli_api_equivalence(tmp_path, monkeypatch, skip_seeders, scenario):
    # Separate working directories keep warehouses and run artifacts independent.
    project = tmp_path / "project"
    write_project(project, scenario)
    documents = []
    reports = []
    tables = []
    failed = scenario == "quality" or (
        scenario in ("missing", "seeder") and (skip_seeders or scenario == "seeder")
    )
    for entrypoint in ("api", "cli"):
        work = tmp_path / entrypoint
        work.mkdir()
        monkeypatch.chdir(work)
        warehouse = work / ".rollout" / "warehouse.duckdb"
        warehouse.parent.mkdir()
        if skip_seeders and scenario not in ("missing", "seeder"):
            with duckdb.connect(str(warehouse)) as connection:
                connection.execute("CREATE SCHEMA staging")
                connection.execute("CREATE TABLE staging.orders AS SELECT 42 AS id")
        if entrypoint == "api":
            result = run_project(str(project), skip_seeders=skip_seeders)
            assert isinstance(result, RunResult)
            assert result.status is (
                ExecutionStatus.FAILED if failed else ExecutionStatus.SUCCESS
            )
            assert result.skip_seeders is skip_seeders
            assert result.report_error is None
            assert result.finished_at >= result.started_at
            assert result.total_duration >= 0
            assert len(result.seeder_results) == (0 if skip_seeders else 1)
            orders = result.model_results[ModelId("public", "orders")]
            assert orders.status is (
                ExecutionStatus.FAILED if failed else ExecutionStatus.SUCCESS
            )
            if scenario in ("quality", "warning"):
                assert orders.quality_results[0].status.value == "FAIL"
            if failed:
                assert (
                    result.model_results[ModelId("public", "report")].status
                    is ExecutionStatus.SKIPPED
                )
        else:
            args = ["run", "--project-dir", str(project)]
            if skip_seeders:
                args.append("--skip-seeders")
            cli = CliRunner().invoke(app, args)
            assert cli.exit_code == int(failed), cli.output
        document = json.loads((warehouse.parent / "state.jsonl").read_text())
        if entrypoint == "api":
            assert document["run_id"] == result.run_id
            assert document["status"] == result.status.value
            assert result.report_path == Path(".rollout/runs") / f"{result.run_id}.md"
        documents.append(normalize_document(document))
        reports.append(
            normalize_report(next((warehouse.parent / "runs").glob("*.md")).read_text())
        )
        with duckdb.connect(str(warehouse)) as connection:
            names = connection.sql(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "ORDER BY table_schema, table_name"
            ).fetchall()
            tables.append(
                {
                    (schema, name): connection.sql(
                        f'SELECT * FROM "{schema}"."{name}"'
                    ).fetchall()
                    for schema, name in names
                }
            )
        assert not (project / ".rollout" / "warehouse.duckdb").exists()
    assert documents[0] == documents[1]
    assert reports[0] == reports[1]
    assert tables[0] == tables[1]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_report_error_is_separate_and_history_is_saved(
    tmp_path, monkeypatch, explicit, fails
):
    models = tmp_path / "models"
    models.mkdir()
    (models / "numbers.sql").write_text(
        "SELECT * FROM missing" if fails else "SELECT 1"
    )
    monkeypatch.chdir(tmp_path)
    if explicit:
        output = "."  # An existing directory cannot be written as a report.
    else:
        output = None
        (tmp_path / ".rollout").mkdir()
        (tmp_path / ".rollout" / "runs").write_text("blocks directory")
    result = run_project(output=output)
    assert result.report_error
    assert result.status is (
        ExecutionStatus.FAILED if fails else ExecutionStatus.SUCCESS
    )
    event = json.loads((tmp_path / ".rollout" / "state.jsonl").read_text())
    assert event["status"] == result.status.value
    assert event["run_id"] == result.run_id


@pytest.mark.parametrize("fails", [False, True])
def test_history_error_is_noncritical(tmp_path, monkeypatch, caplog, fails):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "numbers.sql").write_text(
        "SELECT * FROM missing" if fails else "SELECT 1"
    )
    (tmp_path / ".rollout" / "state.jsonl").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = run_project()
    assert result.status is (
        ExecutionStatus.FAILED if fails else ExecutionStatus.SUCCESS
    )
    assert result.report_path.exists()
    assert result.report_error is None
    assert "Could not persist run state" in caplog.text


@pytest.mark.parametrize(
    "failure, exception",
    [
        ("missing_models", ValueError),
        ("cycle", ValueError),
        ("config", ValueError),
        ("filesystem", OSError),
        ("database", duckdb.Error),
    ],
)
def test_setup_errors_propagate_and_connection_closes(
    tmp_path, monkeypatch, failure, exception
):
    monkeypatch.chdir(tmp_path)
    if failure != "missing_models":
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "numbers.sql").write_text(
            "SELECT * FROM numbers" if failure == "cycle" else "SELECT 1"
        )
    if failure == "config":
        (tmp_path / "models" / "numbers.yaml").write_text("load_strategy: invalid")
    elif failure == "filesystem":
        (tmp_path / ".rollout").write_text("blocks directory")
    elif failure == "database":
        (tmp_path / ".rollout").mkdir()
        (tmp_path / ".rollout" / "warehouse.duckdb").write_text("invalid database")
    with pytest.raises(exception):
        run_project()
    assert not (tmp_path / ".rollout" / "state.jsonl").exists()
    if failure in ("missing_models", "cycle", "config"):
        # Read-only reopening fails if the original read-write connection is still open.
        with duckdb.connect(".rollout/warehouse.duckdb", read_only=True) as connection:
            assert connection.sql("SELECT 1").fetchone() == (1,)


def test_shared_connection_output_path_and_no_console_configuration(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    write_project(project, "success")
    (project / "seeders" / "source.py").write_text(
        "def run(connection, staging_dir):\n"
        "    connection.execute('CREATE TEMP TABLE marker AS SELECT 7 AS id')\n"
        "    return {'orders': connection.sql('SELECT 7 AS id')}\n"
    )
    (project / "models" / "orders.sql").write_text(
        "SELECT * FROM staging.orders JOIN marker USING (id)"
    )
    monkeypatch.chdir(tmp_path)
    package_logger = logging.getLogger("rollout")
    handlers, level = package_logger.handlers[:], package_logger.level
    result = run_project(Path("project"), output=Path("report.md"))
    assert result.status is ExecutionStatus.SUCCESS
    assert result.report_path == Path("report.md")
    assert result.report_path.exists()
    assert not (tmp_path / ".rollout" / "runs").exists()
    assert package_logger.handlers == handlers
    assert package_logger.level == level
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    with duckdb.connect(".rollout/warehouse.duckdb", read_only=True) as connection:
        assert connection.sql("SELECT * FROM public.orders").fetchall() == [(7,)]


def test_skip_seeders_never_imports_or_downloads(tmp_path, monkeypatch):
    write_project(tmp_path, "success")
    (tmp_path / "seeders" / "source.py").write_text("raise RuntimeError('imported')")
    (tmp_path / "seeders" / "source.yaml").write_text("outputs: [invalid")
    monkeypatch.chdir(tmp_path)
    result = run_project(skip_seeders=True)
    assert result.seeder_results == {}
    assert result.status is ExecutionStatus.FAILED
    assert not (tmp_path / ".rollout" / "source").exists()
    assert not result.model_results[ModelId("public", "orders")].quality_results
