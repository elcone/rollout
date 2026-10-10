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
