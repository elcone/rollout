from pathlib import Path

import duckdb
import pytest

from rollout.execution import ExecutionStatus, execute_models


def test_success_in_stored_order(model_dag, engine_config, monkeypatch):
    dag = model_dag(
        {
            "numbers.sql": "SELECT 1 AS number UNION ALL SELECT 2",
            "silver/report.sql": "SELECT * FROM numbers WHERE number = 2",
        }
    )

    def unexpected_sort(*args):
        raise AssertionError("Execution must reuse dag.order")

    monkeypatch.setattr("rollout.dag.topological_sort", unexpected_sort)
    results = execute_models(dag, engine_config)
    assert list(results) == dag.order
    assert all(result.status is ExecutionStatus.SUCCESS for result in results.values())
    assert all(
        isinstance(result.duration, (int, float)) and result.duration >= 0
        for result in results.values()
    )
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM silver.report").fetchall() == [(2,)]


def test_failed_branch_blocks_descendants_and_independent_branch_continues(
    model_dag, engine_config
):
    dag = model_dag(
        {
            "broken/numbers.sql": "SELECT * FROM missing.numbers",
            "silver/report.sql": "SELECT * FROM broken.numbers",
            "gold/report.sql": "SELECT * FROM silver.report",
            "numbers.sql": "SELECT 42 AS number",
            "independent/report.sql": "SELECT * FROM numbers",
        }
    )
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA silver")
        connection.execute("CREATE TABLE silver.report AS SELECT 99 AS stale")
    results = execute_models(dag, engine_config)
    assert {str(identity): result.status for identity, result in results.items()} == {
        "broken.numbers": ExecutionStatus.FAILED,
        "silver.report": ExecutionStatus.SKIPPED,
        "gold.report": ExecutionStatus.SKIPPED,
        "public.numbers": ExecutionStatus.SUCCESS,
        "independent.report": ExecutionStatus.SUCCESS,
    }
    failed = next(
        result for result in results.values() if result.status is ExecutionStatus.FAILED
    )
    assert "missing" in failed.error
    for result in results.values():
        if result.status is ExecutionStatus.SKIPPED:
            assert result.duration is None
            assert result.error is None
        else:
            assert isinstance(result.duration, (int, float))
            assert result.duration >= 0
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM independent.report").fetchall() == [
            (42,)
        ]
        assert connection.execute("SELECT * FROM silver.report").fetchall() == [(99,)]
        assert (
            connection.execute(
                "SELECT * FROM information_schema.tables WHERE table_schema = 'gold'"
            ).fetchall()
            == []
        )


def test_rollback_preserves_committed_target(model_dag, engine_config):
    dag = model_dag({"numbers.sql": "SELECT CAST('invalid' AS INTEGER) AS number"})
    engine_config.warehouse_path.parent.mkdir()
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        connection.execute("CREATE SCHEMA public")
        connection.execute("CREATE TABLE public.numbers AS SELECT 7 AS number")
    results = execute_models(dag, engine_config)
    assert next(iter(results.values())).status is ExecutionStatus.FAILED
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM public.numbers").fetchall() == [(7,)]


def test_artifacts_for_attempted_models_only(model_dag, engine_config):
    dag = model_dag(
        {
            "broken/numbers.sql": "SELECT * FROM missing.numbers",
            "silver/report.sql": "SELECT * FROM broken.numbers",
            "numbers.sql": "SELECT 42",
        }
    )
    execute_models(dag, engine_config)
    run_dir = next((engine_config.warehouse_path.parent / "models").iterdir())
    assert (run_dir / "broken" / "numbers.sql").is_file()
    assert (run_dir / "public" / "numbers.sql").is_file()
    assert not (run_dir / "silver" / "report.sql").exists()


def test_artifacts_preserve_sql_and_previous_runs(model_dag, engine_config, caplog):
    original = "-- original\nSELECT 1 AS number; -- trailing comment"
    dag = model_dag({"silver/numbers.sql": original})
    with caplog.at_level("DEBUG", logger="rollout"):
        execute_models(dag, engine_config)
    artifacts = engine_config.warehouse_path.parent / "models"
    run_dir = next(artifacts.iterdir())
    assert run_dir.name.endswith("Z")
    sql_path = run_dir / "silver" / "numbers.sql"
    saved_sql = sql_path.read_text()
    assert original in saved_sql
    assert str(sql_path) in caplog.text
    assert original not in caplog.text
    with duckdb.connect() as connection:
        connection.execute("CREATE SCHEMA silver")
        connection.execute(saved_sql)
        assert connection.execute("SELECT * FROM silver.numbers").fetchall() == [(1,)]
    execute_models(dag, engine_config)
    assert len(list(artifacts.iterdir())) == 2
    assert sql_path.read_text() == saved_sql


@pytest.mark.parametrize("failure", ["directory", "write"])
def test_artifact_errors_warn_without_blocking_execution(
    model_dag, engine_config, monkeypatch, caplog, failure
):
    dag = model_dag(
        {
            "numbers.sql": "SELECT 1 AS number",
            "silver/report.sql": "SELECT * FROM numbers",
            "broken/numbers.sql": "SELECT * FROM missing.numbers",
        }
    )
    if failure == "directory":
        engine_config.warehouse_path.parent.mkdir()
        (engine_config.warehouse_path.parent / "models").write_text(
            "blocks artifact directory"
        )
    else:

        def fail_write(*args, **kwargs):
            raise OSError("cannot write artifact")

        monkeypatch.setattr(Path, "write_text", fail_write)
    results = execute_models(dag, engine_config)
    assert {str(identity): result.status for identity, result in results.items()} == {
        "public.numbers": ExecutionStatus.SUCCESS,
        "silver.report": ExecutionStatus.SUCCESS,
        "broken.numbers": ExecutionStatus.FAILED,
    }
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 3
    assert all(
        "Could not save generated SQL" in record.getMessage() for record in warnings
    )
    with duckdb.connect(str(engine_config.warehouse_path)) as connection:
        assert connection.execute("SELECT * FROM silver.report").fetchall() == [(1,)]


def test_local_schemas_exist_before_first_model(model_dag, engine_config, monkeypatch):
    import rollout.execution as execution

    dag = model_dag(
        {
            "bronze/numbers.sql": "SELECT 1 AS number",
            "silver/report.sql": "SELECT * FROM bronze.numbers",
            "gold/report.sql": "SELECT * FROM silver.report",
        }
    )
    execute = execution.execute_model
    checked = []

    def inspect_schemas(connection, model, sql):
        schemas = {
            row[0]
            for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata"
            ).fetchall()
        }
        assert {"bronze", "silver", "gold", "public"} <= schemas
        assert "CREATE SCHEMA" not in sql
        checked.append(model.id)
        return execute(connection, model, sql)

    monkeypatch.setattr(execution, "execute_model", inspect_schemas)
    execute_models(dag, engine_config)
    assert checked == dag.order


def test_empty_dag_creates_warehouse(model_dag, engine_config):
    assert execute_models(model_dag({}), engine_config) == {}
    assert engine_config.warehouse_path.is_file()


def test_default_configuration(model_dag, tmp_path, monkeypatch):
    dag = model_dag({"numbers.sql": "SELECT 1"})
    monkeypatch.chdir(tmp_path)
    assert next(iter(execute_models(dag).values())).status is ExecutionStatus.SUCCESS
    assert (tmp_path / ".rollout" / "warehouse.duckdb").is_file()
