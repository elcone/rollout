import pytest
from typer.testing import CliRunner

from rollout.cli import app


def test_dag_stdout_and_output_file(write_models, tmp_path):
    root = write_models({"report.sql": "SELECT * FROM raw.orders"})
    runner = CliRunner()
    result = runner.invoke(app, ["dag", "--models-dir", str(root)])
    assert result.exit_code == 0, result.output
    assert "# Rollout DAG" in result.stdout
    assert "DAG valid" in result.stderr
    output = tmp_path / "dag.md"
    result = runner.invoke(
        app, ["dag", "--models-dir", str(root), "--output", str(output)]
    )
    assert result.exit_code == 0
    assert "raw.orders" in output.read_text()
    assert result.stdout == ""


@pytest.mark.parametrize("fails", [False, True])
def test_run_summary(write_models, tmp_path, monkeypatch, fails):
    files = {
        "staging/customers.sql": "SELECT 1 AS id",
        "silver/customers.sql": "SELECT * FROM staging.customers",
    }
    if fails:
        files.update(
            {
                "silver/orders.sql": "SELECT * FROM missing.orders",
                "gold/report.sql": "SELECT * FROM silver.orders",
            }
        )
    write_models(files)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == int(fails), result.output
    assert "🟢 staging.customers" in result.stdout
    assert "🟢 silver.customers" in result.stdout
    assert "2 successful" in result.stdout
    assert f"{int(fails)} failed" in result.stdout
    assert f"{int(fails)} skipped" in result.stdout
    if fails:
        assert "🔴 silver.orders" in result.stdout
        assert "🟡 gold.report" in result.stdout
    assert (tmp_path / ".rollout" / "warehouse.duckdb").is_file()
    assert len(list((tmp_path / ".rollout" / "runs").glob("*.md"))) == 1


@pytest.mark.parametrize("command", ["dag", "run"])
def test_invalid_dag_returns_one(write_models, tmp_path, monkeypatch, command):
    write_models({"numbers.sql": "SELECT * FROM numbers"})
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, [command])
    assert result.exit_code == 1
    assert "cycle detected" in result.stderr
    assert not (tmp_path / ".rollout").exists()


def test_ignores_empty_models(write_models):
    root = write_models({"empty.sql": "-- comment", "numbers.sql": "SELECT 1"})
    result = CliRunner().invoke(app, ["dag", "--models-dir", str(root)])
    assert result.exit_code == 0
    assert "Models: 1" in result.stdout
    assert "public.empty" not in result.stdout
    assert "Ignoring empty SQL model" in result.stderr


def test_verbose_is_global_and_sql_is_not_printed(write_models, tmp_path, monkeypatch):
    write_models({"numbers.sql": "SELECT 12345 AS number"})
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    verbose = runner.invoke(app, ["--verbose", "run"])
    assert verbose.exit_code == 0
    assert "DEBUG" in verbose.stderr
    assert "Generated SQL saved to" in verbose.stderr
    assert "SELECT 12345" not in verbose.stderr
    normal = runner.invoke(app, ["run"])
    assert normal.exit_code == 0
    assert "DEBUG" not in normal.stderr


def test_run_terminal_colors_and_redirected_output(write_models, tmp_path, monkeypatch):
    write_models({"numbers.sql": "SELECT 1"})
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    colored = runner.invoke(app, ["run"], color=True)
    assert colored.exit_code == 0
    assert "\x1b[32m" in colored.stdout
    plain = runner.invoke(app, ["run"])
    assert plain.exit_code == 0
    assert "\x1b[" not in plain.stdout


def test_dag_output_failure_returns_one(write_models, tmp_path):
    root = write_models({"numbers.sql": "SELECT 1"})
    result = CliRunner().invoke(
        app, ["dag", "--models-dir", str(root), "--output", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_warehouse_setup_failure_returns_one(write_models, tmp_path, monkeypatch):
    write_models({"numbers.sql": "SELECT 1"})
    (tmp_path / ".rollout").write_text("blocks warehouse directory")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == 1
    assert "Error:" in result.stderr


@pytest.mark.parametrize("fails", [False, True])
def test_run_output_report_after_execution(write_models, tmp_path, monkeypatch, fails):
    files = {"numbers.sql": "SELECT 1"}
    if fails:
        files["broken/orders.sql"] = "SELECT * FROM missing.orders"
        files["gold/report.sql"] = "SELECT * FROM broken.orders"
    write_models(files)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "run.md"
    result = CliRunner().invoke(app, ["run", "-o", str(output)])
    assert result.exit_code == int(fails), result.output
    report = output.read_text()
    assert "# Rollout execution" in report
    assert "| public.numbers | SUCCESS |" in report
    assert "1 successful" in result.stdout
    if fails:
        assert "| broken.orders | FAILED |" in report
        assert "missing" in report
        assert "| gold.report | SKIPPED | — | — |  |" in report


def test_run_report_write_failure_returns_one_after_execution(write_models, tmp_path, monkeypatch):
    write_models({"numbers.sql": "SELECT 1"})
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--output", str(tmp_path)])
    assert result.exit_code == 1
    assert "Could not save execution report" in result.stderr
    assert "1 successful" in result.stdout
    assert "Materialized public.numbers" in result.stderr


def test_automatic_report_location_filename_and_previous_runs(write_models, tmp_path, monkeypatch):
    import re
    from datetime import datetime

    write_models({"numbers.sql": "SELECT 1"})
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["run"]).exit_code == 0
    first = next((tmp_path / ".rollout" / "runs").glob("*.md"))
    content = first.read_text()
    assert re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z.md", first.name)
    timestamp = datetime.strptime(first.name, "%Y%m%dT%H%M%S.%fZ.md")
    assert f"Finished at: {timestamp:%Y-%m-%d %H:%M:%S} UTC" in content
    assert "Started at:" in content
    assert re.search(r"^Duration: \d+\.\d{2}s$", content, re.M)
    assert "Generated at" not in content
    assert runner.invoke(app, ["run"]).exit_code == 0
    assert len(list(first.parent.glob("*.md"))) == 2
    assert first.read_text() == content


@pytest.mark.parametrize("failure", ["directory", "write"])
@pytest.mark.parametrize("fails", [False, True])
def test_automatic_report_write_failure_preserves_execution_outcome(
    write_models, tmp_path, monkeypatch, failure, fails
):
    from pathlib import Path

    files = {"numbers.sql": "SELECT 1"}
    if fails:
        files["broken/orders.sql"] = "SELECT * FROM missing.orders"
    write_models(files)
    monkeypatch.chdir(tmp_path)
    runs = tmp_path / ".rollout" / "runs"
    runs.parent.mkdir()
    if failure == "directory":
        runs.write_text("blocks report directory")
    else:
        original_open = Path.open

        def fail_report_open(path, *args, **kwargs):
            if path.parent.name == "runs":
                raise OSError("cannot write report")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_report_open)
    result = CliRunner().invoke(app, ["run"])
    assert result.exit_code == int(fails), result.output
    assert "1 successful" in result.stdout
    assert f"{int(fails)} failed" in result.stdout
    assert "Could not save execution report" in result.stderr
